#!/bin/bash
# Publish one looping video per simulated camera to the RTSP server.
#
# -re is what makes this a camera rather than a file: without it ffmpeg pushes
# frames as fast as it can decode them, the analyzer sees a 400 fps firehose,
# and every latency number in the demo is meaningless.
#
# -stream_loop -1 restarts the file forever. With -c copy there is no re-encode,
# so a dozen of these cost almost nothing and the GPU load stays where it
# belongs -- in the analyzer.
#
# Each camera enters the loop at a different offset. Without that, every camera
# shows the identical part at the identical instant and the whole fleet scores
# the same -- which is exactly what the drawn version did: four cameras whose
# anomaly scores sat within 0.08 of each other, because they were watching
# byte-identical frames. Offsetting costs nothing and makes the fleet a fleet.
set -euo pipefail

RTSP_HOST="${RTSP_HOST:-rtsp-server}"
RTSP_PORT="${RTSP_PORT:-8554}"
CAMERAS="${CAMERAS:-4}"
CATEGORY="${CATEGORY:-pcb1}"
VIDEO="${VIDEO:-/opt/line-${CATEGORY}.mp4}"

if [ ! -f "${VIDEO}" ]; then
  echo "no video at ${VIDEO}" >&2
  echo "available:" >&2
  ls -1 /opt/line-*.mp4 2>/dev/null >&2 || echo "  (none -- the image was built without frames)" >&2
  exit 1
fi

# Seeking is by keyframe, and the video is built with one per second, so the
# offsets land where they say they do.
DURATION="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "${VIDEO}" 2>/dev/null || echo 0)"
DURATION="${DURATION%.*}"
[ "${DURATION:-0}" -gt 0 ] || DURATION=60

echo "publishing ${CAMERAS} cameras of '${CATEGORY}' from ${VIDEO} (${DURATION}s loop) to rtsp://${RTSP_HOST}:${RTSP_PORT}"

pids=()
for i in $(seq 1 "${CAMERAS}"); do
  offset="$(awk -v d="${DURATION}" -v i="$((i - 1))" -v n="${CAMERAS}" \
              'BEGIN { printf "%d", d * i / n }')"
  (
    # Each camera restarts on its own if the server bounces, without taking the
    # others down with it.
    while true; do
      # -loglevel error, not warning. Looping a file with -c copy resets the
      # timestamps on every wrap, and the RTSP muxer warns about non-monotonic
      # DTS once per frame -- 15 lines a second per camera, which buries the
      # startup banner, makes 'make logs' unreadable and fills the node's disk
      # over a long run. ffmpeg corrects the timestamps itself and the stream is
      # fine; the warning is inherent to -stream_loop with -c copy, so it is the
      # one thing here worth suppressing rather than fixing.
      ffmpeg -hide_banner -loglevel error \
             -re -stream_loop -1 -ss "${offset}" -i "${VIDEO}" \
             -c copy -f rtsp -rtsp_transport tcp \
             "rtsp://${RTSP_HOST}:${RTSP_PORT}/cam${i}" || true
      echo "cam${i} publisher exited; restarting in 5s"
      sleep 5
    done
  ) &
  pids+=($!)
  echo "  cam${i} starts at +${offset}s"
done

trap 'kill "${pids[@]}" 2>/dev/null || true' TERM INT
wait
