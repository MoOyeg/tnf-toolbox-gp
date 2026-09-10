#!/usr/bin/env python3
"""Curate a small, committed subset of VisA for the simulated cameras.

Run once by a maintainer, not at build time and not at runtime. VisA is a 1.8 GB
download; what this repository carries is a few hundred resized frames, which is
all a looping demo needs and small enough to live in git and to ship to a bastion
on every 'make app'.

    tar xf VisA_20220922.tar -C /tmp/visa
    ./curate-visa.py --visa /tmp/visa --out frames

Selection is deterministic -- evenly spaced picks from the sorted file list --
so re-running produces the same subset and a diff means the inputs changed
rather than that a shuffle came out differently.

Images are resized to fit the stream, not padded: build-line-video.sh pads to
16:9 at encode time, and doing it once there keeps the committed files smaller.

Masks are deliberately not copied. The .cues file that build-line-video.sh
writes already carries image-level ground truth, which is what an accuracy
number needs; pixel-level masks are weight for a feature that is still an open
question in docs/visual-inspection-redesign.md.
"""
import argparse
import csv
import pathlib
import sys

from PIL import Image

DEFAULT_CATEGORIES = ["pcb1", "capsules"]


def evenly_spaced(items, count):
    """Deterministic sample preserving spread across the sorted list."""
    if count >= len(items):
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def curate_one(src_dir, dest_dir, count, width, height, quality, rows, category, label):
    sources = sorted(p for p in src_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not sources:
        sys.exit(f"no images under {src_dir}")
    picked = evenly_spaced(sources, count)

    dest_dir.mkdir(parents=True, exist_ok=True)
    for existing in dest_dir.glob("*.jpg"):
        existing.unlink()

    for n, src in enumerate(picked):
        with Image.open(src) as im:
            original = im.size
            im = im.convert("RGB")
            im.thumbnail((width, height), Image.LANCZOS)
            dest = dest_dir / f"{n:04d}.jpg"
            im.save(dest, "JPEG", quality=quality, optimize=True)
        rows.append({
            "category": category,
            "label": label,
            "file": f"{category}/{label}/{dest.name}",
            "source": f"{category}/Data/Images/{src.parent.name}/{src.name}",
            "original": f"{original[0]}x{original[1]}",
            "stored": f"{im.size[0]}x{im.size[1]}",
            "bytes": dest.stat().st_size,
        })
    return len(picked)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--visa", required=True, type=pathlib.Path,
                    help="extracted VisA root (the directory holding pcb1/, capsules/, ...)")
    ap.add_argument("--out", default=pathlib.Path("frames"), type=pathlib.Path)
    ap.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES)
    ap.add_argument("--normal", type=int, default=100, help="normal frames per category")
    ap.add_argument("--anomaly", type=int, default=10, help="anomalous frames per category")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--quality", type=int, default=82)
    args = ap.parse_args()

    rows = []
    total = 0
    for category in args.categories:
        images = args.visa / category / "Data" / "Images"
        if not images.is_dir():
            sys.exit(f"{images} not found -- is --visa the directory holding {category}/ ?")
        for label, src_name, count in (("normal", "Normal", args.normal),
                                       ("anomaly", "Anomaly", args.anomaly)):
            n = curate_one(images / src_name, args.out / category / label,
                           count, args.width, args.height, args.quality,
                           rows, category, label)
            print(f"{category}/{label}: {n} frames")
            total += n

    manifest = args.out / "MANIFEST.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    size = sum(r["bytes"] for r in rows)
    print(f"\n{total} frames, {size / 1e6:.1f} MB, manifest at {manifest}")


if __name__ == "__main__":
    main()
