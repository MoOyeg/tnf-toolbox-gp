"""Per-frame defect scoring, with a VLM asked only about the suspicious frames.

Three things happen here, on purpose in this order:

  fast path   every frame, on the GPU, cheap enough to keep up with the camera
  slow path   only when the fast path flags a frame, because a 3B VLM on a T4
              manages a couple of frames a second and a line runs at thirty
  forecast    where the score is heading, which is the part that is actually
              predictive rather than reactive

The fast path is EfficientAD, fitted on good parts only and scoring every frame
against them. It returns a pixel-level anomaly map as well as a number, which is
what makes the result something a person can look at rather than just a reading.
See detector.py for why the fit happens in the pod rather than in the build.

GPU time is charged to the camera whose frame caused it -- see gpu.py. That is
an attribution rather than a measurement, because one device serves every
camera, and it is the number that makes the CAMERAS knob mean something.
"""
import base64
import collections
import os
import pathlib
import threading
import time

import cv2
import numpy as np
import requests
from flask import Flask, Response, jsonify
from prometheus_client import Counter, Gauge, generate_latest

import gpu as gpu_accounting
from detector import EfficientAdDetector

RTSP_HOST = os.environ.get("RTSP_HOST", "rtsp-server")
RTSP_PORT = os.environ.get("RTSP_PORT", "8554")
CAMERAS = int(os.environ.get("CAMERAS", "4"))
VLLM_URL = os.environ.get("VLLM_URL", "http://vllm:8000/v1/chat/completions")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen2.5-VL-3B-Instruct")
VLM_COOLDOWN = float(os.environ.get("VLM_COOLDOWN_SECONDS", "20"))
MODEL_DIR = os.environ.get("MODEL_DIR", "/models")
TRAIN_ROOT = os.environ.get("TRAIN_ROOT", "/train")


def discover_categories():
    """Every line this analyzer has training data for.

    Discovered, not configured. The image is built with one /train/<category>
    per category under camera-sim/frames, and the cameras are published from
    videos built from those same frames -- so both sides agree by construction
    rather than by two environment variables being kept in step by hand.

    CATEGORIES overrides it for a single-line run, which is the only case where
    fitting a model nothing will use is worth avoiding.
    """
    configured = os.environ.get("CATEGORIES", "").strip()
    if configured:
        return [c.strip() for c in configured.split(",") if c.strip()]
    root = pathlib.Path(TRAIN_ROOT)
    return sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []


CATEGORIES = discover_categories()
# Overrides the fitted threshold. Left unset in normal operation: a threshold
# derived from the model's own view of good parts beats one typed into config.
THRESHOLD_OVERRIDE = os.environ.get("ANOMALY_THRESHOLD")
# Supplied by the downward API. Which node the analyzer landed on decides which
# physical card it got, so the two belong together on the page.
NODE_NAME = os.environ.get("NODE_NAME", "unknown")
POD_NAME = os.environ.get("POD_NAME", "unknown")

score_g = Gauge("inspection_anomaly_score", "Current anomaly score", ["camera"])
defects_c = Counter("inspection_defects_total", "Frames over threshold", ["camera"])
reports_c = Counter("inspection_vlm_reports_total", "VLM reports produced", ["camera"])
eta_g = Gauge("inspection_seconds_to_threshold",
              "Forecast seconds until the score crosses the threshold", ["camera"])
# An info metric: always 1, carrying the identity in its labels. Which node and
# which card served a given run is otherwise invisible once the pod is gone.
placement_g = Gauge("inspection_placement_info", "Where the analyzer is running",
                    ["node", "pod", "gpu", "gpu_uuid"])
pass_c = Counter("inspection_pass_total", "Frames judged good", ["camera"])
fail_c = Counter("inspection_fail_total", "Frames judged defective", ["camera"])

app = Flask(__name__)
STATE = {}
LOCK = threading.Lock()


def pick_device():
    """GPU when there is one.

    Without a GPU the pipeline still runs -- EfficientAD scores on CPU -- but
    the fit takes minutes rather than seconds and the throughput story goes
    away. Degrading is better than refusing to start.
    """
    import torch
    if torch.cuda.is_available():
        return torch, torch.device("cuda")
    return torch, torch.device("cpu")


TORCH, DEVICE = pick_device()
GPU = gpu_accounting.GpuAccounting(TORCH, DEVICE)

# One model per line, because a model is only meaningful for the parts it was
# fitted on: EfficientAD learns what a good pcb1 looks like, and scoring a
# capsule against it produces a number that means nothing and looks like it
# means something. Which model a frame gets is decided by the camera's own
# stream name, so a camera can never be scored by the wrong one.
DETECTORS = {
    category: EfficientAdDetector(
        MODEL_DIR, str(pathlib.Path(TRAIN_ROOT) / category), category, DEVICE)
    for category in CATEGORIES
}

# Still one device for all of them. Scoring is serialised so concurrent streams
# cannot interleave on the GPU -- which is also what makes the per-camera
# CUDA-event attribution in gpu.py add up rather than overlap.
INFER_LOCK = threading.Lock()
READY = threading.Event()


def threshold(category):
    """The limit for one line. Each model fits its own from its own good parts."""
    if THRESHOLD_OVERRIDE is not None:
        return float(THRESHOLD_OVERRIDE)
    return DETECTORS[category].threshold


def forecast_seconds(history, limit):
    """Least-squares slope over the recent scores, turned into a time-to-threshold.

    This is the predictive claim, and it is deliberately a modest one: it says
    where the trend is heading if it continues, not that a defect is certain.
    """
    if len(history) < 20:
        return None
    t = np.array([h[0] for h in history], dtype=np.float64)
    v = np.array([h[1] for h in history], dtype=np.float64)
    t = t - t[0]
    slope, intercept = np.polyfit(t, v, 1)
    if slope <= 1e-6:
        return None
    remaining = (limit - v[-1]) / slope
    return max(remaining, 0.0)


def ask_vlm(camera, frame, score):
    """Ask the VLM what is wrong. Only ever called for a flagged frame."""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return None
    b64 = base64.b64encode(buf.tobytes()).decode()
    body = {
        "model": VLLM_MODEL,
        "max_tokens": 160,
        "temperature": 0.2,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text":
                    "You are inspecting parts on a production line. This frame was "
                    f"flagged with an anomaly score of {score:.1f}. Describe the "
                    "defect, rate severity as LOW, MEDIUM or HIGH, and give one "
                    "recommended action. Answer in under 60 words."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
    }
    try:
        r = requests.post(VLLM_URL, json=body, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:                                  # noqa: BLE001
        return f"VLM unavailable: {exc}"


def watch(camera, category):
    """Score one stream against the model for the parts it is watching.

    The category comes from the caller, which took it from the stream name --
    the camera publishing it said what it was, rather than the analyzer working
    it out from a position in a list.
    """
    url = f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{camera}"
    detector = DETECTORS[category]
    history = collections.deque(maxlen=300)
    last_vlm = 0.0
    with LOCK:
        STATE[camera] = {"score": 0.0, "report": None, "eta": None, "jpeg": None,
                         "verdict": None, "threshold": None, "amap_max": None,
                         "category": category}

    while True:
        # FFMPEG/TCP: UDP loses packets under load and the decoder spends its
        # time on corrupt frames rather than real ones.
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            time.sleep(3)
            continue
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            # measure() charges the device time to this camera; the lock
            # keeps concurrent streams from interleaving on one GPU.
            with INFER_LOCK, GPU.measure(camera):
                score, amap = detector.score(frame)
            score_g.labels(camera).set(score)
            history.append((time.time(), score))

            limit = threshold(category)
            eta = forecast_seconds(history, limit)
            if eta is not None:
                eta_g.labels(camera).set(eta)

            # PASS/FAIL is decided here and carried in the state, but nothing
            # draws it yet -- the heatmap and the verdict are phase 3.
            failed = score >= limit
            (fail_c if failed else pass_c).labels(camera).inc()
            entry = {"score": score, "eta": eta, "threshold": limit,
                     "category": category,
                     "verdict": "FAIL" if failed else "PASS",
                     "amap_max": float(amap.max())}
            if failed:
                defects_c.labels(camera).inc()
                # Rate-limited: the VLM is the expensive half, and a defect that
                # lasts twenty frames does not need twenty reports.
                if time.time() - last_vlm > VLM_COOLDOWN:
                    last_vlm = time.time()
                    entry["report"] = ask_vlm(camera, frame, score)
                    reports_c.labels(camera).inc()

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            with LOCK:
                cur = STATE[camera]
                cur.update({k: v for k, v in entry.items() if v is not None})
                cur["score"] = score
                cur["eta"] = eta
                if ok:
                    cur["jpeg"] = buf.tobytes()
        cap.release()


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype="text/plain")


@app.route("/api/state")
def api_state():
    """Per camera: the verdict, the forecast, and what it cost on the GPU."""
    stats = GPU.stats()
    with LOCK:
        out = {c: {k: v for k, v in s.items() if k != "jpeg"}
               for c, s in STATE.items()}
    for camera, entry in out.items():
        entry["gpu"] = stats.get(camera)
    return jsonify(out)


@app.route("/api/runtime")
def api_runtime():
    """Where this analyzer is running, and how the card is doing.

    Reported once for the pod, not per camera, because that is the truth: one
    analyzer, one device, every camera sharing it. What differs per camera is
    the *share* of that device, which is in /api/state.
    """
    return jsonify({
        "node": NODE_NAME,
        "pod": POD_NAME,
        "categories": CATEGORIES,
        "cameras": CAMERAS,
        "ready": READY.is_set(),
        # Per line now: each model fits its own limit from its own good parts,
        # and one number here would have been whichever category sorted first.
        "thresholds": {c: d.threshold for c, d in DETECTORS.items()},
        "gpu": GPU.device_info(),
    })


@app.route("/api/frame/<camera>.jpg")
def api_frame(camera):
    with LOCK:
        jpeg = STATE.get(camera, {}).get("jpeg")
    if not jpeg:
        return Response(status=404)
    return Response(jpeg, mimetype="image/jpeg")


@app.route("/healthz")
def healthz():
    """Not ready until there is a model to score with.

    The first start of a fresh volume fits EfficientAD, which takes minutes. The
    probe failing for that long is correct -- the pod genuinely cannot inspect
    anything yet -- and it is why the readiness probe in the manifest allows for
    it. Serving the endpoint throughout, rather than blocking the port until the
    fit finishes, means the failure reads as "not ready" instead of "connection
    refused".
    """
    if STARTUP_ERROR is not None:
        return Response(f"detector failed: {STARTUP_ERROR}", status=500,
                        mimetype="text/plain")
    if not READY.is_set():
        return Response("fitting", status=503, mimetype="text/plain")
    return "ok"


#: Set when the fit fails, so /healthz can say so instead of answering 503
#: forever with the reason buried in the log.
STARTUP_ERROR = None


def startup():
    """Get a model, then start watching cameras.

    Wrapped, because this runs on a thread: an exception here used to kill that
    thread silently, leaving the pod serving 503 with nothing but a traceback
    in the log to say the fit had died rather than being slow. The two look
    identical from outside, and they need different responses.
    """
    global STARTUP_ERROR
    try:
        _startup()
    except Exception as exc:                                  # noqa: BLE001
        STARTUP_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"detector FAILED to start: {STARTUP_ERROR}", flush=True)
        raise


def camera_plan():
    """Which stream names exist, and the line each one is watching.

    The same split the camera makes: the camera budget divided across the lines,
    remainder to the earlier ones. Both sides compute it from the same category
    list, and the stream name carries the answer either way -- so a drift in the
    counts costs a stream that is never watched, not a stream scored by the
    wrong model.
    """
    if not CATEGORIES:
        return []
    base, extra = divmod(CAMERAS, len(CATEGORIES))
    plan = []
    for index, category in enumerate(CATEGORIES):
        for i in range(1, base + (1 if index < extra else 0) + 1):
            plan.append((f"{category}-{i}", category))
    return plan


def _startup():
    if not CATEGORIES:
        raise RuntimeError(
            f"no categories under {TRAIN_ROOT} -- the image was built without "
            "training frames, so there is nothing to fit a model from")
    # Every line up front rather than on first frame. A fit inside the scoring
    # loop stalls the stream it is meant to be measuring, and the latency
    # numbers are the point of the fast path.
    for category, detector in DETECTORS.items():
        how = detector.ensure()
        print(f"detector {how}: category={category} "
              f"threshold={detector.threshold:.4f} device={DEVICE}", flush=True)
    identity = GPU.identity
    placement_g.labels(node=NODE_NAME, pod=POD_NAME,
                       gpu=identity.get("name", identity.get("device", "cpu")),
                       gpu_uuid=identity.get("uuid", "none")).set(1)
    print(f"placement: node={NODE_NAME} pod={POD_NAME} "
          f"gpu={identity.get('name', 'cpu')} uuid={identity.get('uuid', 'none')}",
          flush=True)
    READY.set()
    for camera, category in camera_plan():
        threading.Thread(target=watch, args=(camera, category), daemon=True).start()


if __name__ == "__main__":
    print(f"analyzer starting: {CAMERAS} cameras, device={DEVICE}, "
          f"categories={','.join(CATEGORIES) or 'none'}", flush=True)
    threading.Thread(target=startup, daemon=True).start()
    threading.Thread(target=GPU.run_forever, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
