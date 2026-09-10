"""EfficientAD: the fast path, fitted on normal parts only.

Trained on good parts alone, which is the honest shape of the problem -- a line
produces good parts, not a labelled catalogue of every way one can fail. What
comes back per frame is a score *and* a pixel-level anomaly map, and the map is
the artifact a person can actually look at.

Why the fit happens here rather than in the image build
-------------------------------------------------------
EfficientAD trains: student/teacher distillation plus an autoencoder. Anomalib's
implementation additionally requires an ImageNette download for the loss penalty
term, with no flag to disable it. OpenShift build pods have no GPU, so baking a
fitted model into the image would mean running a distillation loop on CPU for
hours, and carrying ~1.5 GiB of ImageNette in the build context to do it.

So the fit runs once here, on the card the analyzer already owns, and everything
it produces is cached on a volume. Later starts load in seconds. This is a
deliberate departure from "bake at build time" in the design doc, and the reason
is recorded there too.

The threshold is fitted alongside the weights, from the scores the model gives
the normal set it just learned. An uncalibrated threshold is the thing most
likely to make this look broken -- every frame a defect, or none ever -- so it
is derived from data once and then frozen, rather than guessed in config.
"""
import json
import os
import pathlib
import time

import cv2
import numpy as np
import torch

IMAGE_SIZE = int(os.environ.get("DETECT_IMAGE_SIZE", "256"))
# EfficientAD's implementation expects a batch size of one during training.
FIT_EPOCHS = int(os.environ.get("DETECT_FIT_EPOCHS", "20"))
# Percentile of the normal set's own scores. 99.5 leaves headroom for the
# ordinary spread of good parts without swallowing a real defect.
THRESHOLD_PERCENTILE = float(os.environ.get("DETECT_THRESHOLD_PERCENTILE", "99.5"))


class EfficientAdDetector:
    """Load a cached model, or fit one and cache it."""

    def __init__(self, model_dir, train_dir, category, device):
        self.model_dir = pathlib.Path(model_dir)
        self.train_dir = pathlib.Path(train_dir)
        self.category = category
        self.device = device
        self.model = None
        self.threshold = None
        self.fitted_at = None

    # ---------------------------------------------------------------- paths
    @property
    def _weights(self):
        return self.model_dir / f"{self.category}-efficientad.pt"

    @property
    def _imagenet_dir(self):
        # On the volume, so the ImageNette download happens at most once ever
        # rather than once per pod.
        return self.model_dir / "imagenette"

    # ---------------------------------------------------------------- public
    def ensure(self):
        """Load the cached model, fitting one first if there is none."""
        if self._weights.exists():
            self._load()
            return "loaded"
        self._fit()
        self._load()
        return "fitted"

    def score(self, frame_bgr):
        """Return (image score, anomaly map) for one BGR frame.

        The map comes back at the model's own resolution rather than the
        frame's; drawing it over the frame is the caller's business, and
        phase 3's.
        """
        tensor = self._to_tensor(frame_bgr)
        with torch.no_grad():
            out = self.model.model(tensor)
        score = float(out.pred_score.flatten()[0].item())
        amap = out.anomaly_map[0, 0].detach().cpu().numpy()
        return score, amap

    # ---------------------------------------------------------------- internals
    def _to_tensor(self, frame_bgr):
        # The trained pre-processor is a plain Resize to IMAGE_SIZE; matching it
        # here by hand keeps inference off the datamodule path entirely.
        img = cv2.resize(frame_bgr, (IMAGE_SIZE, IMAGE_SIZE))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        return torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0).to(self.device)

    def _new_model(self):
        from anomalib.models import EfficientAd
        return EfficientAd(imagenet_dir=str(self._imagenet_dir), model_size="small")

    def _load(self):
        blob = torch.load(self._weights, map_location=self.device, weights_only=False)
        model = self._new_model()
        model.model.load_state_dict(blob["state_dict"])
        model.model.to(self.device).eval()
        self.model = model
        self.threshold = blob["threshold"]
        self.fitted_at = blob.get("fitted_at")

    def _fit(self):
        """Fit on the normal frames and derive the threshold from them."""
        from anomalib.data import Folder
        from anomalib.engine import Engine

        started = time.time()
        self.model_dir.mkdir(parents=True, exist_ok=True)
        print(f"detector: no cached model for {self.category}; fitting from "
              f"{self.train_dir} ({FIT_EPOCHS} epochs)", flush=True)

        datamodule = Folder(
            name=self.category,
            root=str(self.train_dir),
            normal_dir="normal",
            train_batch_size=1,
            eval_batch_size=1,
            num_workers=2,
        )
        model = self._new_model()
        engine = Engine(
            default_root_dir=str(self.model_dir / "fit"),
            max_epochs=FIT_EPOCHS,
            accelerator="gpu" if self.device.type == "cuda" else "cpu",
            devices=1,
            logger=False,
            # No enable_checkpointing=False here: anomalib installs its own
            # ModelCheckpoint callback, and Lightning refuses the combination
            # with "found ModelCheckpoint in callbacks list". The checkpoints
            # land under default_root_dir, which is on the model volume, so
            # they cost nothing and are occasionally useful.
            enable_model_summary=False,
        )
        engine.fit(model=model, datamodule=datamodule)

        model.model.to(self.device).eval()
        self.model = model
        threshold = self._calibrate()

        torch.save({
            "state_dict": model.model.state_dict(),
            "threshold": threshold,
            "fitted_at": time.time(),
            "category": self.category,
            "epochs": FIT_EPOCHS,
            "percentile": THRESHOLD_PERCENTILE,
        }, self._weights)
        (self.model_dir / f"{self.category}-fit.json").write_text(json.dumps({
            "category": self.category,
            "threshold": threshold,
            "percentile": THRESHOLD_PERCENTILE,
            "epochs": FIT_EPOCHS,
            "seconds": round(time.time() - started, 1),
        }, indent=2))
        print(f"detector: fitted in {time.time() - started:.0f}s, "
              f"threshold={threshold:.4f}", flush=True)

    def _calibrate(self):
        """Threshold = a high percentile of the normal set's own scores.

        Derived from the same good parts the model was fitted on, so it is the
        model's idea of "normal spread" rather than a number picked in config.
        """
        scores = []
        for path in sorted((self.train_dir / "normal").glob("*.jpg")):
            frame = cv2.imread(str(path))
            if frame is None:
                continue
            with torch.no_grad():
                out = self.model.model(self._to_tensor(frame))
            scores.append(float(out.pred_score.flatten()[0].item()))
        if not scores:
            raise RuntimeError(f"no normal frames under {self.train_dir}/normal "
                               "to calibrate a threshold from")
        return float(np.percentile(scores, THRESHOLD_PERCENTILE))
