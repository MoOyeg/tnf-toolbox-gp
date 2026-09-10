# Visual inspection: from plumbing demo to real inspection

Status: **proposal, for review.** No code has been written against it.

## Why

The pipeline in `app/` works. Frames move from simulated cameras over RTSP to a
GPU fast path, flagged frames go to a VLM, and a dashboard shows the result --
35,620 frames per camera at 2.8 ms each, 411 VLM reports, both T4s claimed.

What it cannot do is demonstrate *visual inspection*, because both halves of the
thing being demonstrated are drawn rather than photographed:

| | today | consequence |
|---|---|---|
| the "part" | a flat rectangle, luma 143 | nothing a viewer recognises as a manufactured object |
| the background | flat colour, luma 35 | 22 populated grey levels in a whole frame |
| the "defect" | an orange rectangle drawn on top | detecting it is colour thresholding, not inspection |
| the VLM prompt | "what is wrong with this part" -- about a rectangle | its answers cannot mean anything |

It also shows up in the numbers: anomaly scores sit at 3.25-3.32 across all four
cameras, because every camera plays an identical, near-featureless file and the
score has nothing to separate.

Making the drawing prettier does not fix this. A belt of five well-lit
rectangles is still rectangles. The imagery and the detector both have to become
real.

## What stays

The architecture is right and is not what is being changed:

- mediamtx and RTSP, so the cameras are simulated at the *protocol* level and
  pointing at real hardware stays a URL change
- the two-tier split -- something cheap on every frame, something expensive only
  on flagged frames. This becomes *more* defensible once the fast path is a real
  detector rather than a statistic over flat colour.
- the Prometheus metrics, the dashboard shell, the 2-GPU split, `make app`

## The shape

```
  camera-sim ──rtsp──> analyzer ──anomaly?──> vLLM (Qwen2.5-VL) ──> report
  (VisA frames)       (EfficientAD,              (defect crop)         │
        │              every frame, GPU)                              │
        │                    │                                        │
        │                    ├── anomaly heatmap ─────────────────────┤
        │                    └── score, PASS/FAIL, metrics ───────────┤
        │                                                             v
   mediamtx <────────────────────── dashboard <──────────────────────-┘
```

Only two boxes change: what `camera-sim` publishes, and what `analyzer` runs.

## 1. Imagery: VisA

[VisA](https://github.com/amazon-science/spot-diff) -- Amazon's Visual Anomaly
dataset. 12 object categories, 10,821 images (9,621 normal, 1,200 anomalous),
with per-image labels and pixel masks for the anomalies.

**Licence: CC BY 4.0**, confirmed from the `LICENSE-DATASET` file shipped in the
tarball ("Attribution 4.0 International"), not just from the project page. This
is why it is the default over MVTec AD, which is
the better-known benchmark but is research/non-commercial and needs
registration. CC BY 4.0 carries an attribution obligation, so the dataset and
its authors get credited in the repository's existing `NOTICE`.

**Categories.** Start with `pcb1` and `capsules`. PCBs read instantly as
manufactured goods, and their defects are small and localised, which is what
makes a heatmap worth showing. Capsules give a second, visually unrelated
category so "point it at a different line" is demonstrable.

**Subset, not the whole thing.** VisA is a 1.8 GB download. A curated few hundred images
per category is plenty for a looping demo and keeps the analyzer image -- already
6.6 GB -- from growing unreasonably. The subset is baked into the `camera-sim`
image at build time.

**Becoming a stream.** No drawing. ffmpeg's concat demuxer plays a directory of
stills, each held for about a second, at a normal:defective ratio around 10:1.
That preserves the property the current generator exists for: the anomaly
arrives on a known cadence, so the fast path, the VLM report and the trend line
can be shown on cue rather than by waiting.

## 2. Detector: EfficientAD

[EfficientAD](https://arxiv.org/abs/2303.14535) (Batzner et al., WACV 2024) via
[Anomalib](https://github.com/openvinotoolkit/anomalib) (Apache 2.0).

Chosen over PatchCore deliberately: it is built for millisecond-level latency,
which means `CAMERAS` can be raised until the T4 is actually saturated. That is
a throughput story the current demo gestures at (`make deploy CAMERAS=16`) but
cannot presently support, because the fast path is not doing real work. The
trade is slightly lower accuracy than PatchCore on the standard benchmarks.

Three properties matter here:

- **Trained on normal images only.** No labelled defects required. This matches
  how a line actually works -- you have good parts, not a catalogue of every way
  one can fail -- and it is the single most credible thing about the design.
- **Outputs a pixel-level anomaly map**, not just a score. That map *is* the
  human-viewable artifact this redesign exists to produce.
- **Small.** It should leave the second T4 entirely to vLLM, which we measured
  at 12.3 GiB resident.

**Threshold calibration is the risk, not the model.** An uncalibrated threshold
is what makes this kind of demo fail visibly: either every frame is a defect or
none is. The threshold is computed once at build time from a held-out set of
normal images -- a high percentile of their scores -- and baked in beside the
weights, so the running system does not have to decide it and cannot drift.

**The fit runs in the pod, not the build. Superseded during implementation.**
This section originally said the weights would be baked at build time, for the
same reason `HF_HUB_OFFLINE=1` bit us in the RHAIIS image -- a network
dependency at startup is a failure that points nowhere near its cause. That
turned out not to be possible for this model, for two reasons found by reading
anomalib rather than by guessing:

- EfficientAD *trains* -- student/teacher distillation plus an autoencoder --
  and OpenShift build pods have no GPU. Baking it would mean a distillation
  loop on CPU.
- Anomalib's implementation requires an ImageNette download (~1.5 GiB) for its
  loss penalty term, and exposes no flag to disable it -- only `imagenet_dir`
  to relocate it.

This is the one thing PatchCore would not have hit: its fit is forward passes
plus coreset selection, no backprop and no penalty dataset, which is exactly
what made it bakeable. The trade EfficientAD was chosen for -- millisecond
inference, so `CAMERAS` can saturate the card -- is unaffected; only the fitting
collides.

So the fit runs once in the analyzer pod, on the T4 it already owns, and the
weights, the threshold and the ImageNette copy are cached on a PersistentVolume.
Later starts load in seconds. `/healthz` answers 503 while fitting, so the pod
reports honestly rather than claiming to be ready, and the readiness probe is
sized for it.

**Latency is to be measured, not assumed.** Published EfficientAD numbers are
from current-generation GPUs; the T4 is Turing and older. The number that
matters is frames/second/camera on *this* hardware, and it goes in the deploy
log once measured.

## 3. Human-viewable output

Per camera, the dashboard shows:

- the photograph of the part
- the anomaly heatmap overlaid on it, alpha-blended
- a box around the largest anomalous region, from connected components on the
  thresholded map
- **PASS** / **FAIL** against the calibrated threshold
- the VLM's sentence
- **its share of the GPU** -- a bar per camera, from `inspection_gpu_share`,
  beside a single device gauge for utilization, memory and power. Per camera is
  the interesting one: it shows the cost of adding a stream, which a
  device-level number alone hides.

The VLM now gets a real defect crop plus the full frame, and is asked what is
wrong with the part and how severe it is. Its answer becomes worth reading,
which it currently is not.

## 4. Metrics and GPU accounting

Keep `inspection_anomaly_score`, `inspection_defects_total`,
`inspection_vlm_reports_total`, `inspection_seconds_to_threshold`,
`inspection_frame_seconds`.

Add throughput and outcome:

- `inspection_frames_per_second{camera}` -- the number the throughput story rests on
- `inspection_pass_total` / `inspection_fail_total{camera}`
- `inspection_detect_seconds` -- fast-path latency, separate from frame handling

VisA ships ground truth, so image-level AUROC and precision/recall at the chosen
threshold are nearly free to compute over the played subset. **Open question**
below -- it converts "it detects flaws" into a number, at the cost of carrying
labels through the pipeline.

### GPU accounting

There are two GPUs doing two different jobs, and they answer the question
differently.

**The analyzer's T4 (fast path).** Per-camera attribution is meaningful here,
because every camera's frames pass through the same model on the same device.
Wrap each inference in CUDA events and attribute the measured *device* time to
the camera whose frame it was:

- `inspection_gpu_seconds_total{camera}` -- counter, device time per camera
- `inspection_gpu_share{camera}` -- gauge, that camera's GPU time over elapsed
  wall time: the fraction of the device it is consuming
- `inspection_detect_seconds{camera}` -- histogram, fast-path latency
- `inspection_frames_per_second{camera}` -- gauge

`torch.cuda.Event(enable_timing=True)` measures time on the device, not the
duration of the Python call. That distinction is the whole reason this is an
attribution rather than a guess: wall-clock timing around an async CUDA call
mostly measures queueing.

**The device itself**, read from NVML in-process (pynvml) and published on the
analyzer's existing `/metrics`:

- `inspection_gpu_utilization`
- `inspection_gpu_memory_used_bytes` / `_total_bytes`
- `inspection_gpu_power_watts`, `inspection_gpu_temperature_celsius`

In-process NVML rather than scraping DCGM, so the dashboard reads one endpoint.
DCGM is still there -- the GPU Operator installs it and `DCGM_FI_DEV_GPU_UTIL`
is already scraped -- and stays the right source for cluster-wide dashboards.
This is deliberately the app's own view of its own device.

**Not per camera: memory.** The model is loaded once and shared across every
stream, so per-camera memory is not separable. Reporting it would be inventing a
number. Memory stays a device metric.

**vLLM's T4 (slow path)** is in another pod, so its device time is invisible to
the analyzer. What the analyzer can honestly attribute is the request:
`inspection_vlm_seconds_total{camera}`, beside the existing
`inspection_vlm_reports_total{camera}`. Device metrics for that GPU come from
DCGM, or from a small NVML exporter in the vLLM pod if it earns a sidecar.

**A caveat that matters for a demo.** We measured `utilization.gpu = 0%` on both
T4s while the pipeline was demonstrably working: nvidia-smi's utilization is a
coarse sample of whether any kernel was resident during a window, and the slow
path is bursty by design. So the per-camera GPU-seconds share is the honest
primary number, and device utilization is a secondary gauge. A dashboard that
leads with the utilization percentage will look broken at idle moments when it
is not.

**This is what gives the `CAMERAS` knob something to show.** Raising it should
visibly raise the summed per-camera GPU seconds until the device saturates and
per-camera fps starts to fall. That is the throughput story EfficientAD was
chosen for, and it is unobservable until these metrics exist.

## 5. What changes in the tree

| Component | Change |
|---|---|
| `app/camera-sim/` | drop the ffmpeg drawing; carry a VisA subset; publish stills via concat |
| `app/analyzer/` | `detector.py` (EfficientAD, fitted in-pod, cached on a PVC, threshold calibrated from the normal set) and `gpu.py` (CUDA-event attribution, NVML device metrics); base image moved to torch 2.6.0 for anomalib |
| `app/dashboard/` | render overlay, box, verdict beside the frame; per-camera GPU bars and a device gauge |
| `app/manifests/` | an `analyzer-models` PVC for the fitted model and ImageNette; readiness probe widened to cover a first fit |
| `deploy/.../roles/app/build.yml` | build context is now `app/` rather than `app/<component>/`, so the analyzer can be fitted on the same frames the cameras publish without a second copy in git |
| `deploy/.../roles/app/` | unchanged -- `make app` builds and deploys this the same way |
| `NOTICE` | VisA attribution, per CC BY 4.0 |

## 6. Staging

Each phase is independently useful and independently reviewable:

1. **Imagery.** VisA through the existing pipeline. Fixes the visible complaint
   -- real photographs instead of grey boxes -- with no model change. The old
   score will behave oddly on real images; that is expected and is what phase 2
   fixes.
2. **Detector, instrumented.** EfficientAD replaces the hand-rolled score, with
   the CUDA-event timing in from the start. Retrofitting per-camera GPU
   attribution onto an uninstrumented inference path is harder than building it
   in, and the numbers are needed to tune the model anyway. PASS/FAIL exists but
   is not yet drawn.
3. **Overlay and GPU view.** Heatmap, box and verdict beside the per-camera GPU
   bars and the device gauge. This is the phase that delivers "human viewable"
   in both senses -- the defect and the cost of finding it.
4. **VLM on real crops.** Re-prompt against the defect region; add
   `inspection_vlm_seconds_total{camera}`.
5. **Throughput.** Raise `CAMERAS` until the T4 saturates, and record where that
   is. This phase is only meaningful because of the accounting added in 2 and 3.
   Optionally add AUROC.

## 7. Open questions

1. **Accuracy reporting** -- worth carrying ground-truth labels through the
   pipeline to publish AUROC and precision/recall, or is the visual enough?
2. **Categories** -- `pcb1` + `capsules` as proposed, or a different pair?
3. **Subset size** -- how many images per category before image size outweighs
   variety? Proposal is a few hundred.
4. **Where the data lives** -- baked into the image (simple, immutable, grows
   the image) or on a PVC populated by a Job (smaller image, another moving
   part). Proposal is baked.
5. **vLLM device metrics** -- worth a small NVML exporter as a sidecar in the
   vLLM pod to get utilization and memory for the second T4 directly, or is
   per-camera request time from the analyzer plus DCGM enough? Proposal is to
   start without the sidecar and add it only if the second GPU turns out to be
   the interesting one.

## 8. Risks

- **Threshold calibration**, as above. The most likely way this looks bad.
- **Image size.** The analyzer is already 6.6 GB; anomalib and a baked model add
  to it. The guest nodes are 200 GiB now, so there is room, but the build itself
  needs transient space and it was eviction that broke the last two builds.
- **EfficientAD accuracy on VisA.** Lower than PatchCore by published numbers.
  If it is visibly weak on the chosen categories, PatchCore is the fallback and
  the interface between analyzer and dashboard does not change.
- **Attribution.** CC BY 4.0 is permissive but not obligation-free.
