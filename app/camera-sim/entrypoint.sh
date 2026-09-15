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

# Every line encoded into the image, not one of them. The build makes a video
# per category under frames/, and publishing only one meant the other sat in the
# image unused while all four cameras showed the same parts offset in time --
# a fleet watching one object type, which is not what a fleet is for.
#
# CATEGORIES overrides the discovery for a single-line demo; unset is the normal
# case and picks up whatever the image was built with.
shopt -s nullglob
videos=( /opt/line-*.mp4 )
shopt -u nullglob

if [ -n "${CATEGORIES:-}" ]; then
  IFS=, read -r -a categories <<< "${CATEGORIES}"
else
  categories=()
  for v in "${videos[@]}"; do
    c="${v##*/line-}"
    categories+=( "${c%.mp4}" )
  done
fi

if [ ${#categories[@]} -eq 0 ]; then
  echo "no videos under /opt -- the image was built without frames" >&2
  exit 1
fi

# Split the camera budget across the lines. A remainder goes to the earlier
# categories rather than being dropped, so CAMERAS=5 over two lines is 3 and 2
# and still adds up to what was asked for.
n_cat=${#categories[@]}
base=$(( CAMERAS / n_cat ))
extra=$(( CAMERAS % n_cat ))
[ "${base}" -gt 0 ] || [ "${extra}" -gt 0 ] || { echo "CAMERAS must be at least 1" >&2; exit 1; }

echo "publishing ${CAMERAS} cameras across ${n_cat} line(s): ${categories[*]}"

pids=()
for ci in "${!categories[@]}"; do
  category="${categories[$ci]}"
  VIDEO="/opt/line-${category}.mp4"
  if [ ! -f "${VIDEO}" ]; then
    echo "no video at ${VIDEO}" >&2
    echo "available:" >&2
    ls -1 /opt/line-*.mp4 2>/dev/null >&2 || echo "  (none)" >&2
    exit 1
  fi

  count=$(( base + (ci < extra ? 1 : 0) ))
  [ "${count}" -gt 0 ] || continue

  # Seeking is by keyframe, and the video is built with one per second, so the
  # offsets land where they say they do.
  DURATION="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "${VIDEO}" 2>/dev/null || echo 0)"
  DURATION="${DURATION%.*}"
  [ "${DURATION:-0}" -gt 0 ] || DURATION=60

  echo "  ${category}: ${count} camera(s) from ${VIDEO} (${DURATION}s loop)"

  for i in $(seq 1 "${count}"); do
    # The stream carries its own category. The analyzer scores each camera with
    # the model for the parts it is actually watching, and reads which that is
    # from the path -- rather than both sides deriving it from an index and
    # agreeing only by luck, which is how capsules end up scored against a pcb1
    # model with nothing saying so.
    cam="${category}-${i}"
    # Offsets are within a line, so two cameras on the same line never show the
    # same part at the same instant.
    offset="$(awk -v d="${DURATION}" -v i="$((i - 1))" -v n="${count}" \
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
               "rtsp://${RTSP_HOST}:${RTSP_PORT}/${cam}" || true
        echo "${cam} publisher exited; restarting in 5s"
        sleep 5
      done
    ) &
    pids+=($!)
    echo "    ${cam} starts at +${offset}s"
  done
done

trap 'kill "${pids[@]}" 2>/dev/null || true' TERM INT
wait
