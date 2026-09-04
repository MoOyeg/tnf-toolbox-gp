"""Per-frame defect scoring, with a VLM asked only about the suspicious frames.

Three things happen here, on purpose in this order:

  fast path   every frame, on the GPU, cheap enough to keep up with the camera
  slow path   only when the fast path flags a frame, because a 3B VLM on a T4
              manages a couple of frames a second and a line runs at thirty
  forecast    where the score is heading, which is the part that is actually
              predictive rather than reactive

The fast path is deliberately not a pretrained model. It builds its own
reference from the first frames each camera sends and scores deviation from it,
which means the container needs no weights, no download and no internet -- it
works in a sandbox with no egress, and it adapts to whatever the camera is
actually pointed at.
"""
import base64
import collections
import os
import threading
import time

import cv2
import numpy as np
import requests
from flask import Flask, Response, jsonify
from prometheus_client import Counter, Gauge, Histogram, generate_latest

RTSP_HOST = os.environ.get("RTSP_HOST", "rtsp-server")
RTSP_PORT = os.environ.get("RTSP_PORT", "8554")
CAMERAS = int(os.environ.get("CAMERAS", "4"))
VLLM_URL = os.environ.get("VLLM_URL", "http://vllm:8000/v1/chat/completions")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen2.5-VL-3B-Instruct")
THRESHOLD = float(os.environ.get("ANOMALY_THRESHOLD", "3.5"))
WARMUP = int(os.environ.get("WARMUP_FRAMES", "90"))
VLM_COOLDOWN = float(os.environ.get("VLM_COOLDOWN_SECONDS", "20"))

score_g = Gauge("inspection_anomaly_score", "Current anomaly score", ["camera"])
defects_c = Counter("inspection_defects_total", "Frames over threshold", ["camera"])
reports_c = Counter("inspection_vlm_reports_total", "VLM reports produced", ["camera"])
eta_g = Gauge("inspection_seconds_to_threshold",
              "Forecast seconds until the score crosses the threshold", ["camera"])
frame_h = Histogram("inspection_frame_seconds", "Fast-path time per frame", ["camera"])

app = Flask(__name__)
STATE = {}
LOCK = threading.Lock()


def torch_device():
    """GPU when there is one. The fast path runs either way, so a missing GPU
    degrades throughput rather than breaking the pipeline."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch, torch.device("cuda")
        return torch, torch.device("cpu")
    except ImportError:
        return None, None


TORCH, DEVICE = torch_device()


class Reference:
    """A running mean and variance per image patch.

    Deviation from it is the anomaly score -- a stripped-down PaDiM, without the
    pretrained backbone. Enough to find a defect the reference has never seen,
    which is the whole job.
    """

    def __init__(self, grid=16):
        self.grid = grid
        self.n = 0
        self.mean = None
        self.m2 = None

    def _patches(self, frame):
        small = cv2.resize(frame, (self.grid * 16, self.grid * 16))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        # Edge energy as well as intensity: a defect that matches the background
        # brightness still disturbs the texture.
        edges = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
        stack = np.stack([gray, np.abs(edges)])
        p = self.grid
        return stack.reshape(2, p, 16, p, 16).mean(axis=(2, 4)).reshape(-1)

    def score(self, frame):
        x = self._patches(frame)
        if TORCH is not None:
            t = TORCH.as_tensor(x, device=DEVICE)
            if self.mean is None:
                self.mean = TORCH.zeros_like(t)
                self.m2 = TORCH.zeros_like(t)
            self.n += 1
            delta = t - self.mean
            self.mean += delta / self.n
            self.m2 += delta * (t - self.mean)
            if self.n < WARMUP:
                return 0.0
            std = TORCH.sqrt(self.m2 / max(self.n - 1, 1)).clamp(min=1e-3)
            return float(((t - self.mean).abs() / std).max().item())

        if self.mean is None:
            self.mean = np.zeros_like(x)
            self.m2 = np.zeros_like(x)
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)
        if self.n < WARMUP:
            return 0.0
        std = np.sqrt(self.m2 / max(self.n - 1, 1)).clip(min=1e-3)
        return float((np.abs(x - self.mean) / std).max())


def forecast_seconds(history):
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
    remaining = (THRESHOLD - v[-1]) / slope
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


def watch(camera):
    url = f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{camera}"
    ref = Reference()
    history = collections.deque(maxlen=300)
    last_vlm = 0.0
    with LOCK:
        STATE[camera] = {"score": 0.0, "report": None, "eta": None, "jpeg": None}

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
            start = time.time()
            score = ref.score(frame)
            frame_h.labels(camera).observe(time.time() - start)
            score_g.labels(camera).set(score)
            history.append((time.time(), score))

            eta = forecast_seconds(history)
            if eta is not None:
                eta_g.labels(camera).set(eta)

            entry = {"score": score, "eta": eta}
            if score >= THRESHOLD:
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
    with LOCK:
        return jsonify({c: {k: v for k, v in s.items() if k != "jpeg"}
                        for c, s in STATE.items()})


@app.route("/api/frame/<camera>.jpg")
def api_frame(camera):
    with LOCK:
        jpeg = STATE.get(camera, {}).get("jpeg")
    if not jpeg:
        return Response(status=404)
    return Response(jpeg, mimetype="image/jpeg")


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    print(f"analyzer starting: {CAMERAS} cameras, device={DEVICE}, "
          f"threshold={THRESHOLD}", flush=True)
    for i in range(1, CAMERAS + 1):
        threading.Thread(target=watch, args=(f"cam{i}",), daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
