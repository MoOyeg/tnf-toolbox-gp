#!/usr/bin/env python3
"""Render docs/architecture.drawio.svg from the layout described below.

The file it writes is a draw.io *editable* SVG: the picture GitHub renders, with
the diagram's own XML carried in the root element's `content` attribute. Opening
it in diagrams.net gives back the editable diagram rather than a flat image.

Which is exactly why it is generated. Hand-editing that file means editing the
drawing and the embedded XML in step, and the two silently drift -- the picture
says one thing and anyone who opens it to edit gets another. Here the boxes and
arrows are data, both outputs come from it, and changing the architecture is an
edit to NODES and EDGES.

    python3 hack/render-architecture.py            # write the SVG
    python3 hack/render-architecture.py --check    # fail if it is out of date

Coordinates are absolute, in the same 1400-wide space draw.io uses, with y
growing downward. Boxes are placed in rows by hand because the diagram is small
enough to read and a layout engine would be more machinery than the problem.
"""

import argparse
import html
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "architecture.drawio.svg")

WIDTH, HEIGHT = 1400, 860

# fill, stroke. Named for what the box *is*, not what colour it happens to be,
# so a box moving between sites keeps its meaning.
PALETTE = {
    "site":    ("#f4f7fb", "#8fa3bf"),   # the dashed container for a whole site
    "infra":   ("#dde8f5", "#5b7fa6"),   # bastions and cluster-level facts
    "compute": ("#e6f2e2", "#5f9150"),   # things that run on the metal
    "hub":     ("#efe6f5", "#8a6aa8"),   # the management hub's own components
    "aws":     ("#fae3e3", "#b06060"),   # outside the clusters entirely
    "app":     ("#fdf0dd", "#c08a35"),   # the inspection app's own pieces
    "note":    ("#ffffff", "#9aa5b1"),   # an annotation, not a component
}

# The two dashed site containers and the guest cluster, drawn first and behind.
CONTAINERS = [
    ("tnf",   30,  60,  700, 450, "TNF site — VPC 10.0.0.0/16"),
    ("acm",   760, 60,  600, 450, "ACM site — VPC 10.1.0.0/16"),
    ("guest", 30,  540, 1330, 300,
     "Guest cluster hcp-1 — control plane and workers both on TNF"),
]

# id, x, y, w, h, palette, lines. First line is the heading.
NODES = [
    # ---------------------------------------------------------------- TNF site
    ("tnfb", 55, 105, 300, 50, "infra", [
        "bastion 10.0.0.5",
        "haproxy · Redfish shim · ignition"]),
    # Small, and inside the site box beside the bastion, because that is the
    # scale of it: the fencing "BMC" is a shim on the bastion turning Redfish
    # calls into EC2 Stop/Start. Drawn in the AWS palette because the endpoint
    # is a regional service, not something running in this VPC.
    ("ec2api", 470, 105, 235, 50, "aws", [
        "AWS EC2 API",
        "Stop / Start — the fencing BMC"]),
    ("m0", 55, 170, 315, 60, "compute", [
        "master-0 · g4dn.metal",
        "8× T4 → vfio-pci"]),
    ("m1", 390, 170, 315, 60, "compute", [
        "master-1 · g4dn.metal",
        "8× T4 → vfio-pci"]),
    ("tnfc", 55, 246, 650, 58, "infra", [
        "Two-node OpenShift with fencing",
        "Pacemaker + etcd · LVM Storage · OpenShift Virtualization · MCE + HyperShift"]),
    # The change this diagram exists to show: the control plane is here, on the
    # metal, not across the peering connection.
    ("hcp", 55, 320, 650, 68, "compute", [
        "Hosted control plane — namespace clusters-hcp-1",
        "etcd · kube-apiserver · konnectivity, as pods on these two nodes",
        "created by TNF's own MultiCluster Engine"]),
    ("vms", 55, 414, 650, 68, "compute", [
        "KubeVirt worker VMs — namespace clusters-hcp-1",
        "3 × guest worker · 2 T4 passed through each · 200Gi root",
        "the same namespace as the control plane that owns them"]),
    # ---------------------------------------------------------------- ACM site
    ("acmb", 785, 105, 300, 50, "infra", [
        "bastion 10.1.0.5",
        "haproxy · ignition"]),
    ("sno", 785, 170, 550, 68, "hub", [
        "sno-0 · m6i.4xlarge — single-node OpenShift",
        "ACM 2.17 hub · MultiCluster Engine · LVM Storage",
        "SiteConfig operator · OpenShift GitOps"]),
    ("fleet", 785, 254, 550, 68, "hub", [
        "Fleet — managed clusters",
        "tnf-gp (imported) · hcp-1 · vcp-1",
        "cluster definitions from Git · Argo CD pull model"]),
    ("s3", 785, 338, 245, 58, "aws", [
        "AWS S3",
        "long-term metrics"]),
    ("mco", 1090, 338, 245, 58, "hub", [
        "MultiCluster Observability",
        "Thanos · right-sizing · Perses"]),
    # Narrower than the boxes above it on purpose: it leaves a clear corridor
    # down the right-hand side for the guest's metrics to reach the hub.
    ("hubnote", 785, 412, 430, 68, "note", [
        "No OpenShift Virtualization here",
        "The hub runs no VMs, so it needs no KVM —",
        "which is why this node is not bare metal."]),
    # ------------------------------------------------------- the inspection app
    ("cam", 60, 585, 220, 78, "app", [
        "camera-sim",
        "VisA stills → looping line",
        "per-camera offsets"]),
    ("rtsp", 340, 585, 180, 78, "app", [
        "rtsp-server",
        "mediamtx",
        "rtsp://…/camN"]),
    ("an", 580, 573, 260, 104, "compute", [
        "analyzer  ·  T4 #1",
        "EfficientAD every frame",
        "CUDA-event GPU accounting",
        "~20.6 ms · fitted threshold"]),
    ("dash", 900, 585, 200, 78, "app", [
        "dashboard",
        "per-camera card",
        "node · gpu · share"]),
    ("vllm", 580, 725, 260, 96, "compute", [
        "vLLM  ·  T4 #2",
        "Qwen2.5-VL-3B (RHAIIS)",
        "only on flagged frames"]),
    ("gmet", 900, 730, 200, 62, "note", [
        "/metrics",
        "inspection_* · DCGM"]),
    ("obs", 1160, 730, 190, 62, "note", [
        "observability addon",
        "allowlisted only"]),
]

# src, src side, dst, dst side, label, dashed. sx/ex pin the x of a vertical
# run so it leaves and arrives under the same point rather than slanting.
EDGES = [
    ("tnfb", "b", "m0", "t", "", False, {"sx": 205, "ex": 205}),
    ("tnfb", "b", "m1", "t", "", False, {"sx": 300}),
    ("tnfb", "r", "ec2api", "l", "Redfish", True, {}),
    ("m0", "b", "tnfc", "t", "", False, {"ex": 212}),
    ("m1", "b", "tnfc", "t", "", False, {"ex": 547}),
    ("tnfc", "b", "hcp", "t", "hosts", False, {}),
    ("hcp", "b", "vms", "t", "NodePool → KubeVirt", False, {}),
    ("acmb", "b", "sno", "t", "", False, {"sx": 935, "ex": 935}),
    ("sno", "b", "fleet", "t", "", False, {"sx": 1060, "ex": 1060}),
    ("fleet", "b", "mco", "t", "", False, {"sx": 1212, "ex": 1212}),
    ("mco", "l", "s3", "r", "blocks", False, {}),
    # The one link between the sites. Dashed, because it is management traffic
    # over VPC peering and not something running across the two.
    ("sno", "l", "tnfc", "r", "manages · VPC peering", True, {}),
    ("cam", "r", "rtsp", "l", "publish", False, {}),
    ("rtsp", "r", "an", "l", "consume", False, {}),
    ("an", "r", "dash", "l", "state", False, {}),
    ("an", "b", "vllm", "t", "flagged frame → report", False, {}),
    ("an", "r", "gmet", "l", "", False, {"sy": 625, "ey": 761}),
    ("gmet", "r", "obs", "l", "scraped", False, {}),
    ("obs", "t", "mco", "b", "to hub", True, {"sx": 1255, "ex": 1255}),
    ("vms", "b", "guest", "t", "the guest cluster runs on these VMs", True,
     {"sx": 380, "ex": 380}),
]

BOX = {n[0]: n[1:5] for n in NODES}
BOX.update({c[0]: c[1:5] for c in CONTAINERS})


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

    for _, x, y, w, h, title in CONTAINERS:
        fill, stroke = PALETTE["site"]
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
                   f'fill="{fill}" stroke="{stroke}" stroke-dasharray="7 5"/>')
        out.append(f'<text x="{x + 14}" y="{y + 24}" font-size="13" '
                   f'font-weight="600" fill="#33445c">{html.escape(title)}</text>')

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

    out.append("</svg>")
    return "\n".join(out) + "\n"


def render_mxfile():
    """The diagram's own XML, for whoever opens the SVG in diagrams.net."""
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']

    def shape(cell_id, x, y, w, h, kind, value, container=False):
        fill, stroke = PALETTE[kind]
        style = (f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};"
                 f"strokeColor={stroke};align=center;verticalAlign=middle;"
                 f"fontSize=11;spacing=4;")
        if container:
            style += "dashed=1;verticalAlign=top;fontStyle=1;"
        return (f'<mxCell id="{cell_id}" value="{html.escape(value, quote=True)}" '
                f'style="{style}" vertex="1" parent="1">'
                f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" '
                f'as="geometry"/></mxCell>')

    for cid, x, y, w, h, title in CONTAINERS:
        cells.append(shape(cid, x, y, w, h, "site", title, container=True))
    for cid, x, y, w, h, kind, lines in NODES:
        cells.append(shape(cid, x, y, w, h, kind, "\n".join(lines)))
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
          f"({len(CONTAINERS)} containers, {len(NODES)} boxes, {len(EDGES)} edges)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
