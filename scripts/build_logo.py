"""Magpie logo — a magpie bringing home a shiny thing, built only from geometric primitives
(circles, a rectangle, triangles, rhombi) combined with boolean operations (union / difference /
intersection). No hand-drawn curves.

This is an *alternative* geometric mark. The extension currently ships a hand-made logo in
extension/icons/ (see extension/README.md), so this script never writes there by default:

    .venv/bin/python scripts/build_logo.py --out DIR              # write icon*.png + svg into DIR
    .venv/bin/python scripts/build_logo.py --out DIR --previews DIR2  # + construction sheet / size previews

Canvas: 128 x 128 design units, y down (SVG convention). Requires shapely + Pillow (both already
in the project's dependency tree via rapidocr / image analysis).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from shapely import affinity
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.ops import unary_union

if "--out" not in sys.argv:
    sys.exit("usage: build_logo.py --out DIR [--previews DIR]  (refuses to overwrite extension/icons implicitly)")
OUT = Path(sys.argv[sys.argv.index("--out") + 1])
PREVIEWS = Path(sys.argv[sys.argv.index("--previews") + 1]) if "--previews" in sys.argv else None
OUT.mkdir(parents=True, exist_ok=True)

INK = (17, 17, 17, 255)        # #111111 — same black as the extension UI
GEM = (232, 168, 46, 255)      # #E8A82E — amber: the shiny thing the magpie brings home
PAPER = (232, 226, 216, 255)   # #E8E2D8 — warm paper white, not screen white (visible on light and dark toolbars)
WHITE = (255, 255, 255, 255)


def circle(cx, cy, r):
    return Point(cx, cy).buffer(r, quad_segs=96)


def rect(x, y, w, h, angle=0.0, origin=None):
    p = Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
    return affinity.rotate(p, angle, origin=origin or "center") if angle else p


def tri(a, b, c):
    return Polygon([a, b, c])


def diamond(cx, cy, r):
    return Polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)])


# ----------------------------------------------------------------- construction
# The bird faces right, head lowered a little, as if it just picked something up.
# All coordinates are in a 128-unit square; y grows downward like the SVG viewport.

# primitives (additive)
HEAD = circle(78, 43, 16)                                   # C1
BODY = circle(54, 68, 26)                                   # C2
NECK = tri((66, 34), (84, 58), (48, 62))                    # T1 — fills the notch between head and body
BEAK = tri((91, 42), (109, 50), (91, 51))                   # T2
# R1 — the long magpie tail: a tapered bar hinged at the rump, pointing down-left
TAIL = Polygon([(0, -7), (54, -3.5), (54, 3.5), (0, 7)])
TAIL = affinity.rotate(TAIL, 147, origin=(0, 0), use_radians=False)
TAIL = affinity.translate(TAIL, 42, 82)
# C6 ∩ C7 — wing: a lens (intersection of two circles) folded along the back, tip toward the tail
WING = circle(42, 84, 20).intersection(circle(68, 66, 24))

# subtractive primitives
EYE = circle(83, 40, 2.6)                                   # C3
# R2 — belly: everything of the body below a slanted line becomes paper (magpie = black cap,
# white belly). The line rises toward the tail.
BELLY_CUT = affinity.rotate(rect(-40, 74, 200, 80), -14, origin=(54, 74))
BELLY = BODY.intersection(BELLY_CUT)

# gem: a rotated square (rhombus) with a smaller rhombus cut out, so it reads as a facetted
# stone and stays a *shape*, not a dot, even at 16 px. Its top vertex sits inside the beak tip
# so it is visibly held, not floating.
GEM_OUT = diamond(110, 60, 9.5)                             # D1
GEM_IN = diamond(110, 60, 3.6)                              # D2
GEM_SHAPE = GEM_OUT.difference(GEM_IN)

# boolean assembly
BIRD = unary_union([HEAD, BODY, NECK, BEAK])
BIRD = BIRD.difference(BELLY).difference(EYE)
BIRD = unary_union([BIRD, TAIL, WING])
# the underside and the eye are explicit paper-coloured layers, not holes: the icon has to read
# on light and dark toolbars alike
PAPER_PARTS = unary_union([BELLY.difference(BIRD), EYE])

LAYERS = [(PAPER_PARTS, PAPER), (BIRD, INK), (GEM_SHAPE, GEM)]
# on a paper tile the underside is true white so it still separates from the tile
TILE = (232, 226, 216, 255)
LAYERS_ON_TILE = [(PAPER_PARTS, WHITE), (BIRD, INK), (GEM_SHAPE, GEM)]

# ----------------------------------------------------------------- export helpers

def polys(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def ring_to_path(coords, nd=2):
    pts = [f"{x:.{nd}f} {y:.{nd}f}" for x, y in coords]
    return "M" + " L".join(pts) + " Z"


def geom_to_path(geom):
    d = []
    for p in polys(geom):
        d.append(ring_to_path(p.exterior.coords))
        for ring in p.interiors:
            d.append(ring_to_path(ring.coords))
    return " ".join(d)


def rgba_hex(c):
    return "#%02x%02x%02x" % c[:3]


def write_svg(path, layers, background=None, size=128, fit_pad=None):
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="{size}" height="{size}">']
    if background:
        parts.append(f'<rect width="128" height="128" rx="28" fill="{background}"/>')
    tf = ""
    if fit_pad is not None:
        minx, miny, maxx, maxy = unary_union([g for g, _ in layers]).bounds
        k = 128 * (1 - 2 * fit_pad) / max(maxx - minx, maxy - miny)
        tx = (128 - (maxx - minx) * k) / 2 - minx * k
        ty = (128 - (maxy - miny) * k) / 2 - miny * k
        tf = f' transform="translate({tx:.3f} {ty:.3f}) scale({k:.4f})"'
    parts.append(f"<g{tf}>")
    for geom, color in layers:
        parts.append(f'<path d="{geom_to_path(geom)}" fill="{rgba_hex(color)}" fill-rule="evenodd"/>')
    parts.append("</g></svg>")
    path.write_text("\n".join(parts))


def render(layers, size, background=None, pad=0.0, ss=8, fit=False):
    """Rasterise with supersampling. `pad` = fraction of the canvas kept as margin.
    `fit` = scale/centre the mark's bounding box inside the canvas instead of using design units."""
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    if background:
        ImageDraw.Draw(img).rounded_rectangle([0, 0, S - 1, S - 1], radius=S * 0.22, fill=background)
    if fit:
        minx, miny, maxx, maxy = unary_union([g for g, _ in layers]).bounds
        scale = S * (1 - 2 * pad) / max(maxx - minx, maxy - miny)
        offx = (S - (maxx - minx) * scale) / 2 - minx * scale
        offy = (S - (maxy - miny) * scale) / 2 - miny * scale
    else:
        scale = S * (1 - 2 * pad) / 128
        offx = offy = S * pad
    for geom, color in layers:
        mask = Image.new("L", (S, S), 0)
        md = ImageDraw.Draw(mask)
        for p in polys(geom):
            md.polygon([(x * scale + offx, y * scale + offy) for x, y in p.exterior.coords], fill=255)
            for ring in p.interiors:
                md.polygon([(x * scale + offx, y * scale + offy) for x, y in ring.coords], fill=0)
        layer = Image.new("RGBA", (S, S), color)
        img.paste(layer, (0, 0), mask)
    return img.resize((size, size), Image.LANCZOS)


def construction_sheet(path):
    """Show the primitives + the result side by side (design rationale)."""
    W, H = 1100, 560
    sheet = Image.new("RGB", (W, H), (246, 244, 240))
    d = ImageDraw.Draw(sheet)
    scale = 3.6
    ox, oy = 40, 60

    def draw_outline(geom, color, width=3):
        for p in polys(geom):
            pts = [(x * scale + ox, y * scale + oy) for x, y in p.exterior.coords]
            d.line(pts + [pts[0]], fill=color, width=width)

    additive = [("C1 head", HEAD), ("C2 body", BODY), ("T1 neck", NECK), ("R1 tail", TAIL), ("T2 beak", BEAK), ("C6∩C7 wing", WING)]
    subtractive = [("C3 eye", EYE), ("R2 belly cut", BELLY)]
    for _, g in additive:
        draw_outline(g, (17, 17, 17))
    for _, g in subtractive:
        draw_outline(g, (200, 60, 60))
    draw_outline(GEM_OUT, (232, 168, 46))
    draw_outline(GEM_IN, (232, 168, 46), 2)
    d.text((ox, 20), "1. primitives — black: union   red: subtract   amber: gem (D1 − D2)", fill=(60, 60, 60))

    ox2 = 600
    d.rounded_rectangle([ox2 - 10, oy - 10, ox2 + 470, oy + 470], radius=24, fill=(214, 210, 202))
    result = render(LAYERS, 460, pad=0.0)
    sheet.paste(result, (ox2, oy), result)
    d.text((ox2, 20), "2. boolean result", fill=(60, 60, 60))
    sheet.save(path)


if __name__ == "__main__":
    # sources of truth (vector, straight from the boolean result)
    write_svg(OUT / "logo.svg", LAYERS)                                           # the mark alone
    write_svg(OUT / "icon.svg", LAYERS_ON_TILE, background="#e8e2d8", fit_pad=0.11)  # mark on tile
    # extension icons: mark on a warm paper tile so it reads on light and dark toolbars alike
    for s in (16, 32, 48, 128):
        render(LAYERS_ON_TILE, s, background=TILE, pad=0.11, fit=True).save(OUT / f"icon{s}.png")
    print("icons ->", OUT)

    if PREVIEWS:
        PREVIEWS.mkdir(parents=True, exist_ok=True)
        render(LAYERS, 512, pad=0.08).save(PREVIEWS / "mark-512.png")
        for name, bgc in (("light", (255, 255, 255, 255)), ("dark", (53, 54, 58, 255))):
            W = 16 + 32 + 48 + 128 + 5 * 24
            bg = Image.new("RGBA", (W, 176), bgc)
            x = 24
            for s in (16, 32, 48, 128):
                bg.alpha_composite(Image.open(OUT / f"icon{s}.png"), (x, 24 + (128 - s) // 2))
                x += s + 24
            bg.save(PREVIEWS / f"sizes-{name}.png")
        construction_sheet(PREVIEWS / "construction.png")
        print("previews ->", PREVIEWS)
