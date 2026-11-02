"""Annotations the agent draws on a capture to point something out, rendered as SVG.

Coordinates are normalized 0..1 with the origin top-left, relative to the image the agent saw:
for an inspect capture of a region, that's the crop, not the whole frame. The SVG embeds the
capture, so the same file works in the dashboard and saved in the session folder.
"""

from __future__ import annotations

import base64
import math
import re
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
            'tip at x2, y2) or "text" (label with its top-left at x, y).',
        },
        **{k: {"type": "number"} for k in ("x", "y", "w", "h", "x1", "y1", "x2", "y2")},
        "label": {"type": "string", "description": f"Short caption drawn next to the shape (up to {MAX_LABEL} characters)."},
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


def render_svg(image: bytes, mime: str, width: int, height: int, annotations: list[dict]) -> str:
    """A standalone SVG: the image with `annotations` (as returned by parse_annotations) drawn over it."""
    W, H = width, height
    size = max(W, H)
    stroke = max(2.0, size * 0.006)
    font = max(12.0, size * 0.035)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'font-family="-apple-system, system-ui, sans-serif" font-weight="700" font-size="{font:.1f}">',
        f'<image href="data:{mime};base64,{base64.b64encode(image).decode()}" width="{W}" height="{H}"/>',
    ]

    def outlined(tag: str, attrs: str, color: str) -> None:
        # A dark halo under the colored stroke keeps it visible on light and dark backgrounds.
        out.append(f'<{tag} {attrs} fill="none" stroke="#000" stroke-opacity=".55" stroke-width="{stroke * 2:.1f}" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        out.append(f'<{tag} {attrs} fill="none" stroke="{color}" stroke-width="{stroke:.1f}" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')

    def text(x: float, y: float, s: str, color: str, anchor: str = "start") -> None:
        out.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" fill="{color}" stroke="#000" '
                   f'stroke-width="{font * 0.2:.1f}" stroke-linejoin="round" paint-order="stroke">{escape(s)}</text>')

    def anchor_for(x: float) -> str:
        return "start" if x < 0.35 else "end" if x > 0.65 else "middle"

    for a in annotations:
        color, label = a["color"], a.get("label")
        if a["shape"] in ("box", "circle"):
            x, y, w, h = a["x"] * W, a["y"] * H, a["w"] * W, a["h"] * H
            if a["shape"] == "box":
                outlined("rect", f'x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{stroke:.1f}"', color)
            else:
                outlined("ellipse", f'cx="{x + w / 2:.1f}" cy="{y + h / 2:.1f}" rx="{w / 2:.1f}" ry="{h / 2:.1f}"', color)
            if label:
                # Above the shape, or below it when it touches the top edge.
                ty = y - stroke * 2 if y - stroke * 2 > font else min(H - stroke, y + h + font + stroke)
                anchor = anchor_for(a["x"] + a["w"] / 2)
                tx = {"start": x, "middle": x + w / 2, "end": x + w}[anchor]
                text(tx, ty, label, color, anchor)
        elif a["shape"] == "arrow":
            x1, y1, x2, y2 = a["x1"] * W, a["y1"] * H, a["x2"] * W, a["y2"] * H
            length = math.hypot(x2 - x1, y2 - y1) or 1.0
            ux, uy = (x2 - x1) / length, (y2 - y1) / length
            head = min(stroke * 5, length * 0.6)
            bx, by = x2 - ux * head, y2 - uy * head  # base of the head
            px, py = -uy * head * 0.55, ux * head * 0.55
            outlined("line", f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{bx:.1f}" y2="{by:.1f}"', color)
            points = f"{x2:.1f},{y2:.1f} {bx + px:.1f},{by + py:.1f} {bx - px:.1f},{by - py:.1f}"
            out.append(f'<polygon points="{points}" fill="{color}" stroke="#000" stroke-opacity=".55" '
                       f'stroke-width="{stroke:.1f}" stroke-linejoin="round" paint-order="stroke"/>')
            if label:
                # At the tail, on the side away from the tip.
                ty = y1 + font + stroke if uy <= 0 else y1 - stroke * 2
                text(x1, min(H - stroke, max(font, ty)), label, color, anchor_for(a["x1"]))
        else:
            # (x, y) is the top-left; near the right edge it's the top-right so the text stays inside.
            text(a["x"] * W, min(H - stroke, a["y"] * H + font * 0.85), a["label"], color,
                 "end" if a["x"] > 0.65 else "start")
    out.append("</svg>")
    return "\n".join(out)
