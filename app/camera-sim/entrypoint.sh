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
set -euo pipefail

RTSP_HOST="${RTSP_HOST:-rtsp-server}"
RTSP_PORT="${RTSP_PORT:-8554}"
CAMERAS="${CAMERAS:-4}"
VIDEO="${VIDEO:-/videos/line.mp4}"

if [ ! -f "$VIDEO" ]; then
  echo "no video at $VIDEO; generating one"
  VIDEO=/tmp/line.mp4 /usr/local/bin/make-sample-video.sh
  VIDEO=/tmp/line.mp4
fi

echo "publishing ${CAMERAS} cameras from ${VIDEO} to rtsp://${RTSP_HOST}:${RTSP_PORT}"

pids=()
for i in $(seq 1 "${CAMERAS}"); do
  (
    # Each camera restarts on its own if the server bounces, without taking the
    # others down with it.
    while true; do
      ffmpeg -hide_banner -loglevel warning \
             -re -stream_loop -1 -i "$VIDEO" \
             -c copy -f rtsp -rtsp_transport tcp \
             "rtsp://${RTSP_HOST}:${RTSP_PORT}/cam${i}" || true
      echo "cam${i} publisher exited; restarting in 5s"
      sleep 5
    done
  ) &
  pids+=($!)
done

trap 'kill "${pids[@]}" 2>/dev/null || true' TERM INT
wait
