#!/bin/bash
# Generate a synthetic production line, so the demo needs no downloaded footage
# and no camera.
#
# A moving belt of parts, and every few seconds one part carries a visible
# defect -- a bright blob. The point is not realism: it is that the anomaly
# arrives on a *known schedule*, so the fast path, the VLM report and the trend
# line can all be demonstrated on cue rather than by waiting for the interesting
# frame to come round again.
set -euo pipefail

OUT="${VIDEO:-/tmp/line.mp4}"
SECONDS_LONG="${SECONDS_LONG:-60}"
FPS="${FPS:-15}"
W="${W:-1280}"
H="${H:-720}"

# Belt: a scrolling gradient. Parts: evenly spaced boxes moving with it.
# Defect: a blob that appears for ~0.6s every 7s, offset so it lands on a part.
ffmpeg -hide_banner -loglevel warning -y \
  -f lavfi -i "color=c=0x202428:s=${W}x${H}:r=${FPS}:d=${SECONDS_LONG}" \
  -f lavfi -i "color=c=0x8a8f98:s=$((W/8))x$((H/3)):r=${FPS}:d=${SECONDS_LONG}" \
  -f lavfi -i "color=c=0xff5a3c:s=44x44:r=${FPS}:d=${SECONDS_LONG}" \
  -filter_complex "\
    [0:v]drawgrid=w=64:h=64:t=1:c=0x2a2f36@0.6[bg]; \
    [bg][1:v]overlay=x='mod(t*180,${W}+200)-200':y=(H-h)/2[belt]; \
    [belt][2:v]overlay=x='mod(t*180,${W}+200)-200+60':y=(H-h)/2+40:\
enable='lt(mod(t\,7)\,0.6)'[out]" \
  -map "[out]" -c:v libx264 -preset veryfast -tune zerolatency \
  -pix_fmt yuv420p -g "${FPS}" "${OUT}"

echo "wrote ${OUT} (${SECONDS_LONG}s, ${W}x${H}@${FPS}, defect every 7s)"
