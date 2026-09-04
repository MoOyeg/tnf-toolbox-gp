# Predictive visual quality inspection

A GPU inspection pipeline that runs on the guest cluster this repo builds. It
watches simulated production-line cameras, scores every frame for defects, and
raises a natural-language inspection report when something looks wrong.

There is no camera in AWS, so the cameras are simulated -- but simulated at the
*protocol* level: a looping video is published to a real RTSP server, and every
component downstream connects to `rtsp://...` exactly as it would to an IP
camera. Nothing in the pipeline knows the difference, and pointing it at real
hardware later is a URL change.

## Why two models, not one

```
                                              ~1-2 fps, only on flagged frames
  camera-sim ──rtsp──> analyzer ──anomaly?──> vLLM (Qwen2.5-VL) ──> report
   (ffmpeg loop)      (GPU, every frame)          (GPU)              │
        │                    │                                      │
        │                    └──────── metrics + trend ─────────────┤
        │                                                           v
   mediamtx <────────────────────── dashboard <─────────────────────┘
```

A vision-language model is the wrong tool for scoring every frame: on a T4 a 3B
VLM manages a couple of frames a second, and a production line runs at 30. It is
the right tool for the handful of frames that are *already suspicious*, where a
sentence of explanation is worth far more than another number.

So the fast path scores every frame on the GPU and is cheap, and the slow path
asks the VLM "what is wrong with this part, and how bad is it" only when the fast
path flags something. That is also what makes the demo affordable: one T4 can
carry both.

The **predictive** part is the third piece: the analyzer tracks the anomaly score
over time per camera and extrapolates, so it reports *"camera 3 is drifting
toward its defect threshold, ~8 minutes out"* before anything is scrapped, rather
than counting rejects after the fact.

## vLLM on a T4 -- read this before changing the model

The T4 is Turing (SM 7.5), and that rules out most defaults:

| | |
|---|---|
| `bfloat16` | **Ampere+ only.** Must run `--dtype float16`; most VLM configs say bf16. |
| FlashAttention 2 | Ampere+ only. vLLM falls back to xformers on Turing. |
| VRAM | 16 GB, and the vision encoder is on top of the weights. |

`Qwen/Qwen2.5-VL-3B-Instruct` in fp16 is roughly 6 GB and leaves room for KV
cache and the vision tower. A 7B model needs 4-bit quantisation to fit, and the
11B Llama vision models do not fit at all. `MODEL` in the Makefile is the knob;
the deployment already pins `--dtype float16` and the xformers backend.

## Red Hat serving

The manifests default to **Red Hat AI Inference Server**, which is vLLM packaged
and supported by Red Hat:

```
registry.redhat.io/rhaiis/vllm-cuda-rhel9:3.2.1
```

It needs a `registry.redhat.io` pull secret in the namespace. If you do not have
one, `make deploy VLLM_IMAGE=vllm/vllm-openai:latest` runs the upstream image --
same API, same flags.

The heavier option is OpenShift AI, whose KServe stack has a vLLM ServingRuntime
and would let you manage the model as an `InferenceService`. That is the right
answer for a platform demo and overkill for one model, so it is not what these
manifests do.

## Running it

```bash
cd app/
make sample-video          # generates a synthetic line, no download needed
make build push            # build the three images
make deploy                # to the guest cluster
make url                   # dashboard route
```

`make deploy` targets whichever cluster `KUBECONFIG` points at:

```bash
export KUBECONFIG=../deploy/clusters/acm/vcp-1.kubeconfig   # guest cluster
export KUBECONFIG=../deploy/clusters/tnf-gp/kubeconfig      # base cluster
```

Both work. The guest cluster is the interesting one -- its GPUs are physical T4s
passed through KubeVirt from the metal.

## Loading the GPU

One camera through the fast path leaves a T4 mostly idle. `CAMERAS` scales the
simulation:

```bash
make deploy CAMERAS=16
```

Each camera is another ffmpeg loop and another analyzer stream. Raise it until
`DCGM_FI_DEV_GPU_UTIL` flattens out -- that number is already being scraped, the
GPU Operator installs the DCGM exporter.
