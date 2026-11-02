"""Annotations the agent draws on a capture to point something out, rendered as SVG.

Coordinates are normalized 0..1 with the origin top-left, relative to the image the agent saw:
for an inspect capture of a region, that's the crop, not the whole frame. The SVG embeds the
capture, so the same file works in the dashboard and saved in the session folder.
"""

from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

SHAPES = ("box", "circle", "arrow", "text")
COLORS = {
    "yellow": "#ffd60a", "red": "#ff3b30", "green": "#34c759", "blue": "#0a84ff", "orange": "#ff9500",
    "magenta": "#ff2d92", "cyan": "#32d2f5", "white": "#ffffff", "black": "#000000",
}
DEFAULT_COLOR = "yellow"
MAX_ANNOTATIONS = 12
MAX_LABEL = 60

ANNOTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "shape": {
            "type": "string",
            "enum": list(SHAPES),
            "description": '"box" (x, y, w, h), "circle" (an ellipse filling x, y, w, h), "arrow" (from x1, y1 to the '
            'tip at x2, y2) or "text" (a note with no mark, shown in the label band nearest x, y).',
        },
        **{k: {"type": "number"} for k in ("x", "y", "w", "h", "x1", "y1", "x2", "y2")},
        "label": {"type": "string", "description": "Short caption, shown above or below the image and joined to the "
                  f"shape by a line (up to {MAX_LABEL} characters). Label the shape itself rather than adding separate text."},
        "color": {"type": "string", "description": f"One of {', '.join(COLORS)} or #rrggbb. Default {DEFAULT_COLOR}."},
    },
    "required": ["shape"],
}


def parse_annotations(items: Any) -> list[dict]:
    """Checks the agent's annotations and returns them normalized; raises ValueError saying what's wrong."""
    if not isinstance(items, list) or not items:
        raise ValueError("annotations must be a non-empty list")
    if len(items) > MAX_ANNOTATIONS:
        raise ValueError(f"at most {MAX_ANNOTATIONS} annotations")
    return [_parse_one(i, a) for i, a in enumerate(items, 1)]


def _parse_one(n: int, a: Any) -> dict:
    if not isinstance(a, dict):
        raise ValueError(f"annotation {n} must be an object")
    shape = a.get("shape")
    if shape not in SHAPES:
        raise ValueError(f"annotation {n}: shape must be one of {', '.join(SHAPES)}")

    def num(k: str) -> float:
        try:
            v = float(a[k])
        except KeyError:
            raise ValueError(f"annotation {n} ({shape}) needs {k}") from None
        except (TypeError, ValueError):
            raise ValueError(f"annotation {n}: {k} must be a number") from None
        if not (math.isfinite(v) and -0.001 <= v <= 1.001):
            raise ValueError(f"annotation {n}: {k}={v} is outside 0..1")
        return min(1.0, max(0.0, v))

    out: dict[str, Any] = {"shape": shape}
    if shape in ("box", "circle"):
        out |= {k: num(k) for k in ("x", "y", "w", "h")}
        if out["w"] <= 0 or out["h"] <= 0:
            raise ValueError(f"annotation {n}: w and h must be positive")
        out["w"], out["h"] = min(out["w"], 1 - out["x"]), min(out["h"], 1 - out["y"])
    elif shape == "arrow":
        out |= {k: num(k) for k in ("x1", "y1", "x2", "y2")}
    else:
        out |= {k: num(k) for k in ("x", "y")}
    label = str(a.get("label") or "").strip()
    if shape == "text" and not label:
        raise ValueError(f"annotation {n}: text needs a label")
    if label:
        out["label"] = label[:MAX_LABEL]
    out["color"] = _color(n, a.get("color"))
    return out


def _color(n: int, color: Any) -> str:
    c = str(color or DEFAULT_COLOR).strip().lower()
    if c in COLORS:
        return COLORS[c]
    if re.fullmatch(r"#[0-9a-f]{6}|#[0-9a-f]{3}", c):
        return c
    raise ValueError(f"annotation {n}: color must be one of {', '.join(COLORS)} or #rrggbb")




Rect = tuple[float, float, float, float]
"""x0, y0, x1, y1 in pixels."""


@dataclass(frozen=True)
class Rendered:
    svg: str
    width: int
    height: int
    """The whole picture, the capture plus the label bands above and below it."""


def render_svg(image: bytes, mime: str, width: int, height: int, annotations: list[dict]) -> Rendered:
    """A standalone SVG: the image with `annotations` (as returned by parse_annotations) drawn over it.

    Only the marks go on the photo. Their labels sit in bands above and below it (marks in the top half
    label upwards, the rest downwards), as chips in the mark's color joined to it by a thin leader line,
    so no label hides any of the capture.
    """
    W, H = width, height
    size = max(W, H)
    stroke = max(2.0, size * 0.005)
    font = max(12.0, size * 0.024)
    shapes: list[str] = []

    def outlined(tag: str, attrs: str, color: str, width: float = stroke) -> None:
        # A dark halo under the colored stroke keeps it visible on light and dark backgrounds.
        shapes.append(f'<{tag} {attrs} fill="none" stroke="#000" stroke-opacity=".55" stroke-width="{width * 2:.1f}" '
                      f'stroke-linejoin="round" stroke-linecap="round"/>')
        shapes.append(f'<{tag} {attrs} fill="none" stroke="{color}" stroke-width="{width:.1f}" '
                      f'stroke-linejoin="round" stroke-linecap="round"/>')

    # The marks, noting for each label what it points at (and whether to draw a leader to it).
    labels: list[tuple[str, str, Rect, bool]] = []  # text, color, target, leader
    for a in annotations:
        color, label = a["color"], a.get("label")
        if a["shape"] in ("box", "circle"):
            x, y, w, h = a["x"] * W, a["y"] * H, a["w"] * W, a["h"] * H
            if a["shape"] == "box":
                outlined("rect", f'x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{stroke:.1f}"', color)
            else:
                outlined("ellipse", f'cx="{x + w / 2:.1f}" cy="{y + h / 2:.1f}" rx="{w / 2:.1f}" ry="{h / 2:.1f}"', color)
            if label:
                labels.append((label, color, (x, y, x + w, y + h), True))
        elif a["shape"] == "arrow":
            x1, y1, x2, y2 = a["x1"] * W, a["y1"] * H, a["x2"] * W, a["y2"] * H
            length = math.hypot(x2 - x1, y2 - y1) or 1.0
            ux, uy = (x2 - x1) / length, (y2 - y1) / length
            head = min(stroke * 5, length * 0.6)
            bx, by = x2 - ux * head, y2 - uy * head  # base of the head
            px, py = -uy * head * 0.55, ux * head * 0.55
            outlined("line", f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{bx:.1f}" y2="{by:.1f}"', color)
            points = f"{x2:.1f},{y2:.1f} {bx + px:.1f},{by + py:.1f} {bx - px:.1f},{by - py:.1f}"
            shapes.append(f'<polygon points="{points}" fill="{color}" stroke="#000" stroke-opacity=".55" '
                          f'stroke-width="{stroke:.1f}" stroke-linejoin="round" paint-order="stroke"/>')
            if label:
                labels.append((label, color, (x1, y1, x1, y1), True))  # from the tail
        else:
            # A note with no mark: in the band, as near its spot as there's room for.
            x, y = a["x"] * W, a["y"] * H
            labels.append((a["label"], color, (x, y, x, y), False))

    # Each band's labels left to right in the order of what they point at, so the leader lines don't cross,
    # in as many rows as it takes (the first nearest the photo).
    chip_h, pad = font * 1.5, font * 0.5
    route = font * 1.2  # between the photo and the first row, where the leader lines bend
    bands: dict[bool, list[tuple[int, Rect, str, str, Rect, bool]]] = {True: [], False: []}  # top? -> placed chips
    for top in (True, False):
        mine = sorted((lb for lb in labels if ((lb[2][1] + lb[2][3]) / 2 < H / 2) == top),
                      key=lambda lb: (lb[2][0] + lb[2][2]) / 2)
        widths = [min(W, _text_size(text, font)[0] + chip_h) for text, *_ in mine]  # the text plus rounded ends
        spots = _lay_out([(t[0] + t[2]) / 2 - w / 2 for (_, _, t, _), w in zip(mine, widths)], widths, W, pad)
        for (text, color, target, leader), w, (r, x) in zip(mine, widths, spots):
            y = -route - chip_h - r * (chip_h + pad) if top else H + route + r * (chip_h + pad)
            bands[top].append((r, (x, y, x + w, y + chip_h), text, color, target, leader))
    nrows = {t: max((r + 1 for r, *_ in placed), default=0) for t, placed in bands.items()}
    top_h, bottom_h = (route + n * (chip_h + pad) if n else 0 for n in (nrows[True], nrows[False]))

    leaders: list[str] = []
    chips: list[str] = []
    for top, placed in bands.items():
        for _, (x0, y0, x1, y1), text, color, target, leader in placed:
            if leader:
                # Out of the chip towards the mark, bending in the gap beside the photo so that over the
                # photo it only runs straight up or down to the mark.
                lx = min(max((target[0] + target[2]) / 2, x0 + chip_h / 2), x1 - chip_h / 2)
                sx = (target[0] + target[2]) / 2
                edge, step = (0, -route / 3) if top else (H, route / 3)
                points = [(lx, y1 if top else y0), (lx, edge + 2 * step), (sx, edge + step), (sx, target[1] if top else target[3])]
                pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
                leaders.append(f'<polyline points="{pts}" fill="none" stroke="#000" stroke-opacity=".55" '
                               f'stroke-width="{stroke * 1.2:.1f}" stroke-linejoin="round" stroke-linecap="round"/>')
                leaders.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{stroke * 0.6:.1f}" '
                               f'stroke-linejoin="round" stroke-linecap="round"/>')
            chips.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{chip_h:.1f}" '
                         f'rx="{chip_h / 2:.1f}" fill="{color}"/>')
            # Squeezed to fit when it's wider than the picture.
            fit = f' textLength="{x1 - x0 - chip_h:.1f}" lengthAdjust="spacingAndGlyphs"' \
                if _text_size(text, font)[0] + chip_h > x1 - x0 else ""
            chips.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{(y0 + y1) / 2:.1f}" dy=".35em" text-anchor="middle" '
                         f'fill="{_ink(color)}"{fit}>{escape(text)}</text>')

    total = H + top_h + bottom_h
    svg = "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {total:.0f}" width="{W}" height="{total:.0f}" '
        f'font-family="-apple-system, system-ui, sans-serif" font-weight="600" font-size="{font:.1f}">',
        f'<rect width="{W}" height="{total:.0f}" fill="#1c1c1e"/>',
        f'<g transform="translate(0 {top_h:.1f})">',
        f'<image href="data:{mime};base64,{base64.b64encode(image).decode()}" width="{W}" height="{H}"/>',
        *shapes, *leaders, *chips,
        "</g>",
        "</svg>",
    ])
    return Rendered(svg, W, round(total))


def _text_size(s: str, font: float) -> tuple[float, float]:
    """Roughly how much room a label takes in the semibold sans-serif it's drawn in."""
    return len(s) * font * 0.6, font * 1.15


def _lay_out(ideals: list[float], widths: list[float], W: float, gap: float) -> list[tuple[int, float]]:
    """(row, x) for chips in left-to-right order, each wanting to start at its `ideals` x: as many as fit
    across `W` per row, then each row pushed apart where they'd overlap and back inside the edges."""
    rows: list[list[int]] = [[]]
    used = 0.0
    for i, w in enumerate(widths):
        if rows[-1] and used + gap + w > W:
            rows.append([])
            used = 0.0
        used += (gap if rows[-1] else 0) + w
        rows[-1].append(i)
    out: list[tuple[int, float]] = [(0, 0.0)] * len(widths)
    for r, row in enumerate(rows):
        xs, right = [], 0.0
        for i in row:
            xs.append(max(ideals[i], right))
            right = xs[-1] + widths[i] + gap
        limit = W
        for k in reversed(range(len(row))):
            xs[k] = max(0.0, min(xs[k], limit - widths[row[k]]))
            limit = xs[k] - gap
        for k, i in enumerate(row):
            out[i] = (r, xs[k])
    return out


def _ink(color: str) -> str:
    """Black or white, whichever reads better on `color` (#rgb or #rrggbb)."""
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#000" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#fff"
