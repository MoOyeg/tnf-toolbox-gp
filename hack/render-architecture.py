#!/usr/bin/env python3
"""Render docs/architecture.drawio.svg from the layout described below.

The file it writes is a draw.io *editable* SVG: the picture GitHub renders, with
the diagram's own XML carried in the root element's `content` attribute. Opening
it in diagrams.net gives back the editable diagram rather than a flat image.

Which is exactly why it is generated. Hand-editing that file means editing the
drawing and the embedded XML in step, and the two silently drift -- the picture
says one thing and anyone who opens it to edit gets another. Here the boxes and
arrows are data, both outputs come from it, and changing the architecture is an
edit to CONTAINERS, NODES and EDGES.

    python3 hack/render-architecture.py            # write the SVG
    python3 hack/render-architecture.py --check    # fail if it is out of date

The diagram nests three kinds of boundary, and telling them apart is most of
what it is for (the app area is a fourth style, but a workload rather than a
boundary -- it is drawn inside whichever guest was built):

    site      a VPC, dashed -- an AWS network boundary, not a cluster
      cluster an OpenShift cluster, solid blue
        guest a cluster hosted *by* the cluster it is drawn inside, solid green

Both guest profiles are drawn inside the TNF cluster, side by side, because
both run there and a site definition picks one of them:

    hcp-1     control plane as pods on TNF, workers as KubeVirt VMs on TNF
    vcp-1     every node a KubeVirt VM on TNF, no hosted control plane at all

They are alternatives, not layers -- EXCLUSION keeps either from being drawn
inside the other, which would say one is built on top of the other. Drawing
either as a *peer* of TNF -- which this diagram used to do -- is the picture of
the old topology, where the control plane lived on the ACM hub.

Coordinates are absolute, in the same space draw.io uses, with y growing
downward. Boxes are placed by hand because the diagram is small enough to read
and a layout engine would be more machinery than the problem.
"""

import argparse
import html
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "architecture.drawio.svg")

WIDTH, HEIGHT = 1400, 930

# fill, stroke. Named for what the box *is*, not what colour it happens to be,
# so a box moving between sites keeps its meaning.
PALETTE = {
    "infra":   ("#dde8f5", "#5b7fa6"),   # bastions and cluster-level facts
    "compute": ("#e6f2e2", "#5f9150"),   # things that run on the metal
    "hub":     ("#efe6f5", "#8a6aa8"),   # the management hub's own components
    "aws":     ("#fae3e3", "#b06060"),   # outside the clusters entirely
    "app":     ("#fdf0dd", "#c08a35"),   # the inspection app's own pieces
    "note":    ("#ffffff", "#9aa5b1"),   # an annotation, not a component
}

# fill, stroke, dashed, stroke width, title colour. The whole point of this
# table is that a reader can tell a VPC from a cluster at a glance.
BOUNDARY = {
    "site":    ("#f7f9fc", "#9fb0c9", True,  1.2, "#5a6b82"),
    "cluster": ("#eaf1f9", "#3f6390", False, 2.2, "#2b4a70"),
    "guest":   ("#edf6ea", "#4a7c3f", False, 2.2, "#35602b"),
    # Not a boundary of its own -- it is a workload, drawn inside whichever
    # guest was built. Dashed and quiet so it does not read as a third cluster.
    "app-area": ("#fdf8f0", "#c09a5c", True, 1.6, "#7a5a1e"),
}

# id, x, y, w, h, kind, label. Outermost first -- they are drawn in this order
# so an inner boundary paints over the one containing it.
CONTAINERS = [
    ("tnfsite", 30, 60, 900, 790, "site",
     "TNF site — VPC 10.0.0.0/16 · peered with the ACM VPC"),
    # The peering is stated once, on the wider site -- this title has 410px
    # and the long form runs off the canvas.
    ("acmsite", 960, 60, 410, 790, "site", "ACM site — VPC 10.1.0.0/16"),
    ("tnfcluster", 55, 175, 850, 655, "cluster",
     "TNF cluster — two-node OpenShift 4.22 with fencing"),
    # The two guest profiles, side by side because they are alternatives: a
    # site definition names one or the other. Both run entirely on TNF.
    ("hcp1", 75, 378, 400, 170, "guest",
     "hcp-1 — hosted control plane"),
    ("vcp1", 495, 378, 390, 170, "guest",
     "vcp-1 — every node a VM"),
    ("app", 75, 576, 810, 240, "app-area",
     "The inspection app — deployed into whichever guest was built"),
    ("acmcluster", 985, 175, 360, 385, "cluster",
     "ACM hub — single-node OpenShift"),
]


# id, x, y, w, h, palette, lines. First line is the heading.
NODES = [
    # --------------------------------------------------- TNF site, outside the cluster
    ("tnfb", 55, 105, 345, 50, "infra", [
        "bastion 10.0.0.5",
        "haproxy · Redfish shim · ignition"]),
    ("ec2api", 470, 105, 270, 50, "aws", [
        "AWS EC2 API",
        "Stop / Start — the fencing BMC"]),
    # ------------------------------------------------------------- the TNF cluster
    ("m0", 75, 215, 395, 60, "compute", [
        "master-0 · g4dn.metal",
        "8× T4 → vfio-pci"]),
    ("m1", 490, 215, 395, 60, "compute", [
        "master-1 · g4dn.metal",
        "8× T4 → vfio-pci"]),
    # The second line is the either/or the two green boxes below cannot say for
    # themselves: drawn side by side, they would otherwise read as coexisting.
    ("tnfsvc", 75, 292, 810, 56, "infra", [
        "Pacemaker + etcd · LVM Storage · OpenShift Virtualization · MCE + HyperShift",
        "a site definition picks one guest profile — hcp-1 or vcp-1, never both"]),
    # ------------------------------------------- hcp-1: control plane as pods on TNF
    ("cp", 95, 424, 360, 50, "compute", [
        "Control plane — pods on the two nodes",
        "etcd · kube-apiserver · konnectivity"]),
    ("hvm", 95, 486, 360, 50, "compute", [
        "3 × worker VM · 2 T4 each",
        "created by HyperShift from a NodePool"]),
    # ------------------------------------------ vcp-1: every node a VM, no hosted CP
    ("vvm", 515, 424, 350, 50, "compute", [
        "3 × control-plane VM · 1 T4 each",
        "these are the cluster — no pods on TNF"]),
    ("vnote", 515, 486, 350, 50, "note", [
        "Installed by the assisted installer",
        "own storage disk · survives the hub"]),
    # ------------------------------------------------------- the app, in either guest
    ("cam", 95, 622, 170, 76, "app", [
        "camera-sim",
        "VisA stills → looping line",
        "per-camera offsets"]),
    ("rtsp", 320, 622, 120, 76, "app", [
        "rtsp-server",
        "mediamtx",
        "rtsp://…/camN"]),
    ("an", 495, 612, 210, 88, "compute", [
        "analyzer  ·  T4",
        "EfficientAD every frame",
        "CUDA-event GPU accounting",
        "~20.6 ms · fitted threshold"]),
    ("dash", 745, 622, 120, 76, "app", [
        "dashboard",
        "per-camera card",
        "node · gpu · share"]),
    ("vllm", 495, 724, 210, 76, "compute", [
        "vLLM  ·  T4",
        "Qwen2.5-VL-3B (RHAIIS)",
        "only on flagged frames"]),
    ("gmet", 745, 724, 120, 76, "note", [
        "/metrics",
        "inspection_* · DCGM",
        "→ observability addon"]),
    # ---------------------------------------------------------------- the ACM site
    ("acmb", 985, 105, 360, 50, "infra", [
        "bastion 10.1.0.5",
        "haproxy · ignition"]),
    ("sno", 1005, 215, 320, 56, "hub", [
        "sno-0 · m6i.4xlarge",
        "16 vCPU · 64 GiB · no virtualization"]),
    ("acmhub", 1005, 291, 320, 68, "hub", [
        "ACM 2.17 hub · MultiCluster Engine",
        "SiteConfig operator · OpenShift GitOps",
        "LVM Storage"]),
    ("fleet", 1005, 379, 320, 68, "hub", [
        "Fleet — managed clusters",
        "tnf-gp (imported) · the guest it built",
        "cluster definitions from Git · Argo CD"]),
    ("mco", 1005, 487, 320, 56, "hub", [
        "MultiCluster Observability",
        "Thanos · right-sizing · Perses"]),
    ("s3", 985, 630, 360, 56, "aws", [
        "AWS S3",
        "long-term metrics"]),
    ("hubnote", 985, 720, 360, 68, "note", [
        "No OpenShift Virtualization here",
        "the hub runs no VMs, so it needs no KVM,",
        "and so does not need to be bare metal"]),
]


# src, src side, dst, dst side, label, dashed, pins. sx/sy/ex/ey pin a
# coordinate so a run leaves and arrives where it should rather than slanting.
EDGES = [
    ("tnfb", "b", "m0", "t", "", False, {"sx": 200, "ex": 200}),
    ("tnfb", "b", "m1", "t", "", False, {"sx": 300}),
    ("tnfb", "r", "ec2api", "l", "Redfish", True, {}),
    ("m0", "b", "tnfsvc", "t", "", False, {"ex": 272}),
    ("m1", "b", "tnfsvc", "t", "", False, {"ex": 687}),
    # One or the other. A site definition names a profile, and the same two
    # nodes below carry whichever it names.
    ("tnfsvc", "b", "hcp1", "t", "HyperShift", False, {"sx": 275, "ex": 275}),
    ("tnfsvc", "b", "vcp1", "t", "assisted installer", False,
     {"sx": 690, "ex": 690}),
    # Into whichever exists.
    ("hcp1", "b", "app", "t", "", True, {"sx": 275, "ex": 275}),
    ("vcp1", "b", "app", "t", "", True, {"sx": 690, "ex": 690}),
    ("cam", "r", "rtsp", "l", "publish", False, {}),
    ("rtsp", "r", "an", "l", "consume", False, {}),
    ("an", "r", "dash", "l", "state", False, {}),
    ("an", "b", "vllm", "t", "flagged frame → report", False, {}),
    ("an", "r", "gmet", "l", "", False, {"sy": 690}),
    ("acmb", "b", "sno", "t", "", False, {"sx": 1165, "ex": 1165}),
    ("sno", "b", "acmhub", "t", "", False, {"sx": 1165, "ex": 1165}),
    ("acmhub", "b", "fleet", "t", "", False, {"sx": 1165, "ex": 1165}),
    ("fleet", "b", "mco", "t", "", False, {"sx": 1212, "ex": 1212}),
    # Out of the cluster and into object storage, so it crosses the boundary.
    ("mco", "b", "s3", "t", "blocks", False, {"sx": 1165, "ex": 1165}),
    # The one link between the sites, and it is management only. The label has
    # to fit the clear band between master-1 and sno-0 -- the sites are 30px
    # apart, so anything longer gets painted over by a box. Peering is stated
    # in the TNF site's title instead.
    ("sno", "l", "tnfcluster", "r", "builds · manages", True, {"ey": 320}),
    ("gmet", "r", "mco", "l", "to hub", True, {}),
]


BOX = {n[0]: n[1:5] for n in NODES}
BOX.update({c[0]: c[1:5] for c in CONTAINERS})

LEGEND_Y = 878
LEGEND = [
    (30, "site", "VPC / site boundary"),
    (300, "cluster", "OpenShift cluster"),
    (560, "guest", "cluster hosted by the cluster it sits inside"),
    (900, "app-area", "the inspection app — a workload, not a cluster"),
]


# Which box must sit geometrically inside which. The nesting *is* the argument
# the diagram makes -- the guests running on TNF rather than beside it -- so a
# box nudged out of its boundary is a wrong diagram, not a cosmetic slip.
CONTAINMENT = [
    ("tnfcluster", "tnfsite"),
    ("hcp1", "tnfcluster"),
    ("vcp1", "tnfcluster"),
    ("app", "tnfcluster"),
    ("acmcluster", "acmsite"),
    ("cp", "hcp1"), ("hvm", "hcp1"),
    ("vvm", "vcp1"), ("vnote", "vcp1"),
    ("cam", "app"), ("rtsp", "app"), ("an", "app"), ("dash", "app"),
    ("vllm", "app"), ("gmet", "app"),
    ("m0", "tnfcluster"), ("m1", "tnfcluster"), ("tnfsvc", "tnfcluster"),
    ("sno", "acmcluster"), ("acmhub", "acmcluster"),
    ("fleet", "acmcluster"), ("mco", "acmcluster"),
]


# And which must NOT: the bastion is not part of the cluster it installs, and
# S3 is not part of the hub.
EXCLUSION = [
    # The two profiles are alternatives, so neither may be drawn inside the
    # other -- that would read as one built on top of the other.
    ("hcp1", "vcp1"),
    ("vcp1", "hcp1"),
    ("tnfb", "tnfcluster"),
    ("ec2api", "tnfcluster"),
    ("acmb", "acmcluster"),
    ("s3", "acmcluster"),
    ("hubnote", "acmcluster"),
]


def _inside(inner, outer):
    ax, ay, aw, ah = BOX[inner]
    bx, by, bw, bh = BOX[outer]
    return bx <= ax and by <= ay and ax + aw <= bx + bw and ay + ah <= by + bh


def validate():
    """Fail on a layout that draws something the architecture does not do."""
    problems = []
    for inner, outer in CONTAINMENT:
        if not _inside(inner, outer):
            problems.append(f"{inner} must be drawn inside {outer}")
    for inner, outer in EXCLUSION:
        if _inside(inner, outer):
            problems.append(f"{inner} must NOT be drawn inside {outer}")
    for box in BOX:
        x, y, w, h = BOX[box]
        if x < 0 or y < 0 or x + w > WIDTH or y + h > HEIGHT:
            problems.append(f"{box} falls outside the {WIDTH}x{HEIGHT} canvas")
    for src, _, dst, _, _, _, _ in EDGES:
        for end in (src, dst):
            if end not in BOX:
                problems.append(f"edge endpoint {end} is not a box")
    return problems


def anchor(node_id, side, override_x=None, override_y=None):
    x, y, w, h = BOX[node_id]
    point = {
        "t": (x + w / 2, y),
        "b": (x + w / 2, y + h),
        "l": (x, y + h / 2),
        "r": (x + w, y + h / 2),
    }[side]
    return (override_x if override_x is not None else point[0],
            override_y if override_y is not None else point[1])


def label_box(text, cx, cy):
    """Geometry for an edge label: a white plate the line runs behind."""
    width = 6.2 * len(text) + 8
    return width, cx - width / 2, cy - 7.5


def render_svg():
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'content="{html.escape(render_mxfile(), quote=True)}">',
        '<defs><marker id="a" markerWidth="9" markerHeight="9" refX="8" refY="3" '
        'orient="auto"><path d="M0,0 L0,6 L8,3 z" fill="#5b6b7c"/></marker></defs>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
        '<style>text{font-family:ui-sans-serif,system-ui,-apple-system,'
        'Segoe UI,Roboto,sans-serif}</style>',
    ]

    for _, x, y, w, h, kind, title in CONTAINERS:
        fill, stroke, dashed, stroke_w, title_fill = BOUNDARY[kind]
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
                   f'fill="{fill}" stroke="{stroke}" '
                   f'stroke-width="{stroke_w}"{dash}/>')
        out.append(f'<text x="{x + 14}" y="{y + 24}" font-size="13" '
                   f'font-weight="600" fill="{title_fill}">'
                   f'{html.escape(title)}</text>')

    # Edges before boxes, so a line never sits on top of a shape.
    for src, s_side, dst, d_side, text, dashed, pin in EDGES:
        sx, sy = anchor(src, s_side, pin.get("sx"), pin.get("sy"))
        ex, ey = anchor(dst, d_side, pin.get("ex"), pin.get("ey"))
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        out.append(f'<path d="M{sx},{sy} L{ex},{ey}" fill="none" '
                   f'stroke="#5b6b7c" stroke-width="1.4"{dash} '
                   f'marker-end="url(#a)"/>')
        if text:
            cx, cy = (sx + ex) / 2, (sy + ey) / 2
            width, lx, ly = label_box(text, cx, cy)
            out.append(f'<rect x="{lx:.1f}" y="{ly:.1f}" width="{width:.1f}" '
                       f'height="15" rx="3" fill="#ffffff" opacity="0.92"/>')
            out.append(f'<text x="{cx:.1f}" y="{ly + 11:.1f}" font-size="9.5" '
                       f'text-anchor="middle" fill="#4a5b6d">'
                       f'{html.escape(text)}</text>')

    for _, x, y, w, h, kind, lines in NODES:
        fill, stroke = PALETTE[kind]
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" '
                   f'fill="{fill}" stroke="{stroke}"/>')
        cx = x + w / 2
        out.append(f'<text x="{cx:.1f}" y="{y + 24}" font-size="11.5" '
                   f'font-weight="600" text-anchor="middle" fill="#1f2933">'
                   f'{html.escape(lines[0])}</text>')
        # 15px to the first detail line, 13px between the rest -- the heading
        # is a point larger and needs the extra leading.
        offset = 39
        for line in lines[1:]:
            out.append(f'<text x="{cx:.1f}" y="{y + offset}" font-size="10" '
                       f'text-anchor="middle" fill="#52606d">'
                       f'{html.escape(line)}</text>')
            offset += 13

    # Four box styles is three more than a reader should have to infer.
    for x, kind, text in LEGEND:
        fill, stroke, dashed, stroke_w, _ = BOUNDARY[kind]
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        out.append(f'<rect x="{x}" y="{LEGEND_Y}" width="34" height="20" rx="5" '
                   f'fill="{fill}" stroke="{stroke}" '
                   f'stroke-width="{stroke_w}"{dash}/>')
        out.append(f'<text x="{x + 44}" y="{LEGEND_Y + 14}" font-size="10.5" '
                   f'fill="#52606d">{html.escape(text)}</text>')

    out.append("</svg>")
    return "\n".join(out) + "\n"


def render_mxfile():
    """The diagram's own XML, for whoever opens the SVG in diagrams.net."""
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']

    def shape(cell_id, x, y, w, h, style, value):
        return (f'<mxCell id="{cell_id}" value="{html.escape(value, quote=True)}" '
                f'style="{style}" vertex="1" parent="1">'
                f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" '
                f'as="geometry"/></mxCell>')

    for cid, x, y, w, h, kind, title in CONTAINERS:
        fill, stroke, dashed, stroke_w, _ = BOUNDARY[kind]
        style = (f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};"
                 f"strokeColor={stroke};align=center;verticalAlign=top;"
                 f"fontSize=11;spacing=4;fontStyle=1;"
                 f"strokeWidth={stroke_w};dashed={1 if dashed else 0};")
        cells.append(shape(cid, x, y, w, h, style, title))

    for cid, x, y, w, h, kind, lines in NODES:
        fill, stroke = PALETTE[kind]
        style = (f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};"
                 f"strokeColor={stroke};align=center;verticalAlign=middle;"
                 f"fontSize=11;spacing=4;")
        cells.append(shape(cid, x, y, w, h, style, "\n".join(lines)))

    for i, (src, _, dst, _, text, dashed, _) in enumerate(EDGES):
        style = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;"
                 "endArrow=block;endFill=1;strokeColor=#5b6b7c;")
        if dashed:
            style += "dashed=1;"
        cells.append(f'<mxCell id="e{i}" value="{html.escape(text, quote=True)}" '
                     f'style="{style}" edge="1" parent="1" '
                     f'source="{src}" target="{dst}">'
                     f'<mxGeometry relative="1" as="geometry"/></mxCell>')

    return ('<mxfile host="app.diagrams.net"><diagram name="architecture">'
            f'<mxGraphModel dx="{WIDTH}" dy="900" grid="0" page="1" '
            f'pageWidth="{WIDTH}" pageHeight="{HEIGHT}">'
            f'<root>{"".join(cells)}</root>'
            "</mxGraphModel></diagram></mxfile>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed SVG is not what this renders")
    args = parser.parse_args()

    problems = validate()
    if problems:
        for problem in problems:
            print(f"layout error: {problem}", file=sys.stderr)
        return 1

    svg = render_svg()
    if args.check:
        with open(OUT, encoding="utf-8") as handle:
            current = handle.read()
        if current != svg:
            print(f"{OUT} is out of date -- run hack/render-architecture.py",
                  file=sys.stderr)
            return 1
        print(f"{os.path.relpath(OUT, ROOT)} is up to date.")
        return 0

    with open(OUT, "w", encoding="utf-8") as handle:
        handle.write(svg)
    print(f"wrote {os.path.relpath(OUT, ROOT)} "
          f"({len(CONTAINERS)} boundaries, {len(NODES)} boxes, {len(EDGES)} edges)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
