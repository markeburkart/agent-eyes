"""Multi-step instructions: one marked-up capture per step, shown as one group the user steps through.

Each step has its own capture (an overview, or an inspect of the part it's about) and marks only
that step. Earlier steps' marks can be drawn in grey for context, on any capture from the same
camera: they're moved from their capture's coordinates to this one's through the full frame both
were cut from (`earlier_marks`), and left out where they fall outside it.
"""

from __future__ import annotations

from typing import Any

from ..camera import Capture
from .annotate import ANNOTATION_SCHEMA, MAX_ANNOTATIONS, parse_annotations

MAX_STEPS = 12
MAX_TITLE = 60
MAX_SAY = 300
EARLIER_COLOR = "#a8a8a8"

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": f'A few words naming the step, e.g. "Ground". Up to {MAX_TITLE} characters.'},
        "say": {"type": "string", "description": "The step as you would say it aloud, one or two short sentences. "
                "Shown under the image and read out when the user moves to the step."},
        "capture": {"type": "integer", "description": "Number of the capture to mark up for this step. Omit for the latest one."},
        "annotations": {
            "type": "array",
            "description": f"Up to {MAX_ANNOTATIONS} marks for this step only (none for a step with nothing to point at). "
            "Coordinates are normalized 0..1 on this step's capture, as for annotate_capture.",
            "items": ANNOTATION_SCHEMA,
        },
    },
    "required": ["title", "say", "annotations"],
}


def parse_steps(items: Any, latest: int | None) -> list[dict]:
    """Checks the agent's steps; returns [{title, say, capture, annotations}] with annotations normalized
    and `capture` defaulting to `latest`. Raises ValueError saying what's wrong."""
    if not isinstance(items, list) or len(items) < 2:
        raise ValueError("steps must be a list of at least 2 steps (use annotate_capture for one)")
    if len(items) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps")
    out = []
    for n, s in enumerate(items, 1):
        if not isinstance(s, dict):
            raise ValueError(f"step {n} must be an object")
        title = str(s.get("title") or "").strip()[:MAX_TITLE]
        say = str(s.get("say") or "").strip()[:MAX_SAY]
        if not title or not say:
            raise ValueError(f"step {n} needs a title and what to say")
        number = s.get("capture") or latest
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError(f"step {n}: capture must be a capture number")
        marks = s.get("annotations") or []
        try:
            annotations = parse_annotations(marks) if marks else []
        except ValueError as e:
            raise ValueError(f"step {n}: {e}") from None
        out.append({"title": title, "say": say, "capture": number, "annotations": annotations})
    return out


def earlier_marks(annotations: list[dict], src: Capture, dst: Capture) -> list[dict]:
    """`annotations` made on capture `src`, moved onto capture `dst` and greyed out, without labels.

    Both captures must be from the same camera. Text marks are dropped (they're only a label), as are
    marks that leave `dst`'s image; boxes are cut to it."""
    if src.camera.id != dst.camera.id:
        return []
    sr, dr = src.region, dst.region
    sx, sy, sw, sh = (sr.x, sr.y, sr.w, sr.h) if sr else (0.0, 0.0, 1.0, 1.0)
    dx, dy, dw, dh = (dr.x, dr.y, dr.w, dr.h) if dr else (0.0, 0.0, 1.0, 1.0)

    def px(x: float) -> float:
        return (sx + x * sw - dx) / dw

    def py(y: float) -> float:
        return (sy + y * sh - dy) / dh

    def inside(v: float) -> bool:
        return -1e-6 <= v <= 1 + 1e-6

    out = []
    for a in annotations:
        m: dict[str, Any] = {"shape": a["shape"], "color": EARLIER_COLOR}
        if a["shape"] == "box":
            x0, y0 = max(0.0, px(a["x"])), max(0.0, py(a["y"]))
            x1, y1 = min(1.0, px(a["x"] + a["w"])), min(1.0, py(a["y"] + a["h"]))
            if x1 - x0 < 0.005 or y1 - y0 < 0.005:
                continue
            m |= {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
        elif a["shape"] == "circle":
            x0, y0, x1, y1 = px(a["x"]), py(a["y"]), px(a["x"] + a["w"]), py(a["y"] + a["h"])
            if not all(map(inside, (x0, y0, x1, y1))):
                continue
            m |= {"x": max(0.0, x0), "y": max(0.0, y0), "w": min(1.0, x1) - max(0.0, x0), "h": min(1.0, y1) - max(0.0, y0)}
        elif a["shape"] == "arrow":
            pts = px(a["x1"]), py(a["y1"]), px(a["x2"]), py(a["y2"])
            if not all(map(inside, pts)):
                continue
            m |= dict(zip(("x1", "y1", "x2", "y2"), (min(1.0, max(0.0, v)) for v in pts)))
        else:
            continue
        out.append({k: round(v, 4) if isinstance(v, float) else v for k, v in m.items()})
    return out
