"""Per-camera GPU accounting, and the device's own view of itself.

Two different questions, answered by two different instruments:

  per camera   how much of the GPU is this stream costing? Nothing in NVML can
               answer that -- one device runs one model for every camera -- so
               it is an *attribution*: CUDA events time the work on the device
               and the result is charged to the camera whose frame it was.

  per device   utilization, memory, power, temperature. Read from NVML in this
               process rather than scraped from DCGM, so the dashboard has one
               endpoint to read and the app owns its own view of its own card.

Why CUDA events and not time.time(): a CUDA call is asynchronous, so wall clock
around it mostly measures queueing. Events are recorded on the stream and
measure work on the device, which is the only number that can honestly be
divided between cameras.

The cost is a synchronise per measured block. That is deliberate -- an
unsynchronised elapsed_time is a lie -- and it is why measurement is something
the caller opts into per frame rather than something wrapped around everything.
"""
import collections
import threading
import time
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram

# Attributed to a camera.
gpu_seconds_c = Counter("inspection_gpu_seconds_total",
                        "GPU device time attributed to a camera", ["camera"])
gpu_share_g = Gauge("inspection_gpu_share",
                    "Fraction of the device a camera is consuming", ["camera"])
detect_h = Histogram("inspection_detect_seconds",
                     "Fast-path detection time per frame", ["camera"])
fps_g = Gauge("inspection_frames_per_second", "Frames scored per second", ["camera"])

# The device itself. No camera label -- these are not divisible.
util_g = Gauge("inspection_gpu_utilization", "Device busy percent")
mem_used_g = Gauge("inspection_gpu_memory_used_bytes", "Device memory in use")
mem_total_g = Gauge("inspection_gpu_memory_total_bytes", "Device memory installed")
power_g = Gauge("inspection_gpu_power_watts", "Device power draw")
temp_g = Gauge("inspection_gpu_temperature_celsius", "Device temperature")


class GpuAccounting:
    """Charges device time to cameras, and polls the device for its own state."""

    def __init__(self, torch=None, device=None, window=30.0):
        self.torch = torch
        self.device = device
        self.cuda = bool(torch is not None and device is not None
                         and getattr(device, "type", "cpu") == "cuda")
        self.window = window
        self._lock = threading.Lock()
        # Per camera: seconds of device time and frames, since the last rollup.
        self._pending = {}
        # Recent per-frame device times, for percentiles the dashboard can show
        # without scraping and quantiling a Prometheus histogram itself.
        self._recent = collections.defaultdict(lambda: collections.deque(maxlen=240))
        self._latest = {}
        self._started = time.time()
        self.identity = {"device": "cpu"}
        self._nvml = self._open_nvml()

    # ---------------------------------------------------------------- nvml
    def _open_nvml(self):
        if not self.cuda:
            return None
        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem_total_g.set(pynvml.nvmlDeviceGetMemoryInfo(handle).total)

            def text(value):
                return value.decode() if isinstance(value, bytes) else str(value)

            # Which physical card this is. Every camera shares it -- there is
            # one model on one device -- so this is reported once for the pod
            # rather than pretended to be per camera.
            self.identity = {
                "device": "cuda",
                "name": text(pynvml.nvmlDeviceGetName(handle)),
                "uuid": text(pynvml.nvmlDeviceGetUUID(handle)),
                "index": 0,
                "pci": text(pynvml.nvmlDeviceGetPciInfo(handle).busId),
                "memory_total": int(pynvml.nvmlDeviceGetMemoryInfo(handle).total),
                "driver": text(pynvml.nvmlSystemGetDriverVersion()),
            }
            return (pynvml, handle)
        except Exception:                                     # noqa: BLE001
            # Device metrics are a nice-to-have; per-camera attribution is the
            # number that matters and does not depend on NVML being present.
            return None

    def poll_device(self):
        if self._nvml is None:
            return
        pynvml, handle = self._nvml
        try:
            util_g.set(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)
            mem_used_g.set(pynvml.nvmlDeviceGetMemoryInfo(handle).used)
            power_g.set(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0)
            temp_g.set(pynvml.nvmlDeviceGetTemperature(handle, 0))
        except Exception:                                     # noqa: BLE001
            pass

    # ---------------------------------------------------------------- timing
    @contextmanager
    def measure(self, camera):
        """Time one frame's work and charge it to the camera.

        Falls back to wall clock without CUDA, where the two are the same thing
        because the work is synchronous anyway.
        """
        if not self.cuda:
            t0 = time.perf_counter()
            yield
            self._record(camera, time.perf_counter() - t0)
            return

        start = self.torch.cuda.Event(enable_timing=True)
        end = self.torch.cuda.Event(enable_timing=True)
        start.record()
        try:
            yield
        finally:
            end.record()
            end.synchronize()
            self._record(camera, start.elapsed_time(end) / 1000.0)

    def _record(self, camera, seconds):
        gpu_seconds_c.labels(camera).inc(seconds)
        detect_h.labels(camera).observe(seconds)
        with self._lock:
            slot = self._pending.setdefault(camera, [0.0, 0])
            slot[0] += seconds
            slot[1] += 1
            self._recent[camera].append(seconds)

    # ---------------------------------------------------------------- rollup
    def rollup(self):
        """Turn accumulated time into rates. Called on a timer, not per frame.

        share is device time over elapsed wall time, so 0.5 means the camera is
        keeping the GPU busy half the time. Summed across cameras it approaches
        1.0 as the device saturates, which is the number the CAMERAS knob is
        supposed to move.
        """
        now = time.time()
        with self._lock:
            elapsed = max(now - self._started, 1e-6)
            pending, self._pending, self._started = self._pending, {}, now
        for camera, (seconds, frames) in pending.items():
            share, fps = seconds / elapsed, frames / elapsed
            gpu_share_g.labels(camera).set(share)
            fps_g.labels(camera).set(fps)
            with self._lock:
                self._latest[camera] = {"share": share, "fps": fps}

    def stats(self):
        """Per-camera GPU numbers, for the dashboard.

        share and fps come from the last rollup rather than being recomputed,
        so the page and the Prometheus gauges cannot disagree. The percentiles
        are over the recent window -- p95 is the number that shows a camera
        occasionally waiting on the device, which a mean hides.
        """
        out = {}
        with self._lock:
            for camera, recent in self._recent.items():
                times = sorted(recent)
                latest = self._latest.get(camera, {})
                if not times:
                    continue
                out[camera] = {
                    "share": latest.get("share"),
                    "fps": latest.get("fps"),
                    "ms_p50": times[len(times) // 2] * 1000.0,
                    "ms_p95": times[min(int(len(times) * 0.95), len(times) - 1)] * 1000.0,
                    "seconds_total": sum(recent),
                }
        return out

    def device_info(self):
        """The card's own current state, alongside which card it is.

        Not called device(): self.device is the torch device this was
        constructed with, and an attribute of that name shadows the method.
        """
        snapshot = dict(self.identity)
        if self._nvml is not None:
            pynvml, handle = self._nvml
            try:
                snapshot.update({
                    "utilization": pynvml.nvmlDeviceGetUtilizationRates(handle).gpu,
                    "memory_used": int(pynvml.nvmlDeviceGetMemoryInfo(handle).used),
                    "power_watts": pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0,
                    "temperature": pynvml.nvmlDeviceGetTemperature(handle, 0),
                })
            except Exception:                                 # noqa: BLE001
                pass
        return snapshot

    def run_forever(self, interval=10.0):
        while True:
            time.sleep(interval)
            self.poll_device()
            self.rollup()
