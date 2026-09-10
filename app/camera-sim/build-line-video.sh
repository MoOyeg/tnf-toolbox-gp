#!/bin/bash
# Turn a directory of photographs into a looping production line.
#
# Run at image build time, not at container start. The encode is deterministic,
# so paying for it once in the build means pods start instantly, every replica
# plays byte-identical frames, and nothing has to be generated on a node that is
# already carrying a 6.6 GiB analyzer image.
#
# The cadence is the point of the ordering. Anomalous parts are placed every
# DEFECT_EVERY positions, so a defect arrives on a known schedule and the fast
# path, the VLM report and the trend line can be demonstrated on cue rather than
# by waiting for an interesting frame to come round again.
#
# A .cues file is written beside the video recording which second holds which
# part and whether it was anomalous. Nothing reads it yet; it is what lets a
# later phase score detections against ground truth without re-deriving the
# order from the video.
set -euo pipefail

CATEGORY="${1:?usage: build-line-video.sh <category> [outfile]}"
OUT="${2:-/opt/line-${CATEGORY}.mp4}"
FRAMES="${FRAMES_DIR:-/opt/frames}/${CATEGORY}"

HOLD="${HOLD:-1.0}"                 # seconds each part dwells in view
FPS="${FPS:-15}"
W="${W:-1280}"
H="${H:-720}"
DEFECT_EVERY="${DEFECT_EVERY:-11}"  # one anomalous part per N parts

[ -d "${FRAMES}/normal" ]  || { echo "no ${FRAMES}/normal" >&2; exit 1; }
[ -d "${FRAMES}/anomaly" ] || { echo "no ${FRAMES}/anomaly" >&2; exit 1; }

shopt -s nullglob
normal=( "${FRAMES}"/normal/*.jpg )
anomaly=( "${FRAMES}"/anomaly/*.jpg )
shopt -u nullglob

[ ${#normal[@]}  -gt 0 ] || { echo "no normal frames in ${FRAMES}" >&2; exit 1; }
[ ${#anomaly[@]} -gt 0 ] || { echo "no anomalous frames in ${FRAMES}" >&2; exit 1; }

list="$(mktemp)"; cues="${OUT%.mp4}.cues"
: > "${cues}"
trap 'rm -f "${list}"' EXIT

# Centiseconds, in integers. ubi-minimal has no bc, and floating point in shell
# is not worth a dependency for accumulating a timestamp.
HOLD_CS="$(awk -v h="${HOLD}" 'BEGIN { printf "%d", h * 100 }')"
[ "${HOLD_CS}" -gt 0 ] || { echo "HOLD must be > 0" >&2; exit 1; }

# Walk the normal parts in order, dropping an anomalous one in every
# DEFECT_EVERY positions. Anomalies cycle, so a short anomaly set still spreads
# across the whole loop instead of repeating one part.
pos=0; ai=0; t_cs=0
emit() {  # emit <path> <label>
  printf "file '%s'\nduration %s\n" "$1" "${HOLD}" >> "${list}"
  printf '%d,%d.%02d,%s,%s\n' "${pos}" "$((t_cs / 100))" "$((t_cs % 100))" \
         "$2" "$(basename "$1")" >> "${cues}"
  pos=$((pos + 1)); t_cs=$((t_cs + HOLD_CS))
}

for f in "${normal[@]}"; do
  if [ $(( pos % DEFECT_EVERY )) -eq $(( DEFECT_EVERY - 1 )) ]; then
    emit "${anomaly[$(( ai % ${#anomaly[@]} ))]}" anomaly
    ai=$((ai + 1))
  fi
  emit "${f}" normal
done
# The concat demuxer gives the final entry no duration unless it is repeated.
printf "file '%s'\n" "${normal[-1]}" >> "${list}"

# Scale to fit and pad rather than crop: a defect can be anywhere on the part,
# and cropping to 16:9 would quietly cut the top and bottom off every frame.
ffmpeg -hide_banner -loglevel warning -y \
  -f concat -safe 0 -i "${list}" \
  -vf "scale=w=${W}:h=${H}:force_original_aspect_ratio=decrease,\
pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:color=0x20242a,fps=${FPS},format=yuv420p" \
  -c:v libx264 -preset veryfast -tune stillimage -crf 20 -g "${FPS}" \
  "${OUT}"

echo "wrote ${OUT}: ${pos} parts, $(grep -c ,anomaly, "${cues}") anomalous, ${HOLD}s each, ${W}x${H}@${FPS}"
