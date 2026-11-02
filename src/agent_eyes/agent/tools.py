"""In-process MCP tools the agent uses to look at the bench."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..camera import CameraError, CameraSource, Capture, Region
from .annotate import ANNOTATION_SCHEMA, MAX_ANNOTATIONS
from .budget import CaptureBudget
from .steps import MAX_STEPS, STEP_SCHEMA

SERVER_NAME = "eyes"
CAPTURE_TOOL = f"mcp__{SERVER_NAME}__capture_frame"
ANNOTATE_TOOL = f"mcp__{SERVER_NAME}__annotate_capture"
STEPS_TOOL = f"mcp__{SERVER_NAME}__show_steps"

ShowPopover = Callable[[str, str, dict], str]
"""show_popover(kind, title, payload) -> popover id: opens a popover on the dashboard; raises ValueError
for a payload it can't show. Kinds so far: "annotated-image" ({"capture": n, "annotations": [...], "caption": str})
and "steps" ({"steps": [{"title", "say", "capture", "annotations"}, ...], "show_earlier": bool})."""

CAPTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "Why you need to look, in a few words. Shown to the user."},
        "view": {
            "type": "string",
            "description": 'Which camera. Use "front": the main camera, looking down at the work area.',
        },
        "detail": {
            "type": "string",
            "enum": ["overview", "inspect"],
            "description": '"overview" for a cheap look at the whole scene; "inspect" for full resolution of `region`.',
        },
        "region": {
            "type": "object",
            "description": "Part of the frame to capture, normalized 0..1 with the origin top-left, as seen in an earlier "
            "overview of the same view. Omit for the full frame.",
            "properties": {k: {"type": "number"} for k in ("x", "y", "w", "h")},
            "required": ["x", "y", "w", "h"],
        },
    },
    "required": ["reason", "view", "detail"],
}


class CaptureLog:
    """Captures taken so far, numbered from 1, so the agent can refer to one later (e.g. to annotate it).
    Shared across conversations by whoever keeps it, so the numbers stay unique."""

    def __init__(self) -> None:
        self._captures: list[Capture] = []

    def add(self, cap: Capture) -> int:
        self._captures.append(cap)
        return len(self._captures)

    def get(self, number: int) -> Capture | None:
        return self._captures[number - 1] if 1 <= number <= len(self._captures) else None

    @property
    def latest(self) -> int | None:
        return len(self._captures) or None


@dataclass(frozen=True)
class CaptureEvent:
    """One capture_frame call, for whoever displays them (terminal now, dashboard later)."""

    reason: str
    view: str
    detail: str
    capture: Capture | None = None
    number: int | None = None
    """The capture's number in the CaptureLog."""
    error: str | None = None
    """Error code ("throttled", "no_frames", ...) when nothing was captured."""
    message: str = ""


ANNOTATE_SCHEMA = {
    "type": "object",
    "properties": {
        "capture": {"type": "integer", "description": "Number of the capture to mark up. Omit for the latest one."},
        "title": {"type": "string", "description": "A few words, shown as the popover's title."},
        "caption": {"type": "string", "description": "Optional one-line note shown under the image."},
        "annotations": {
            "type": "array",
            "description": f"Up to {MAX_ANNOTATIONS} marks. Coordinates are normalized 0..1 from the top-left of "
            "that capture's image as you saw it: for an inspect of a region, relative to the crop, not the full frame.",
            "items": ANNOTATION_SCHEMA,
        },
    },
    "required": ["title", "annotations"],
}

STEPS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": 'What the steps do, in a few words, e.g. "Wire the PN532 to the Pico".'},
        "steps": {"type": "array", "description": f"2 to {MAX_STEPS} steps, in order.", "items": STEP_SCHEMA},
        "show_earlier": {"type": "boolean", "description": "Draw earlier steps' marks in grey on each step's image, "
                         "for context. Default true."},
    },
    "required": ["title", "steps"],
}


def capture_server(
    camera: CameraSource,
    budget: CaptureBudget,
    on_capture: Callable[[CaptureEvent], None],
    captures: CaptureLog,
    show_popover: ShowPopover | None = None,
):
    """The eyes MCP server: capture_frame, plus annotate_capture when there is a dashboard (`show_popover`)."""

    @tool(
        "capture_frame",
        "Take one picture of the workbench. Only look when the answer depends on the current physical state, "
        "or when the user asks you to look. Start with an overview; use inspect with a region to read small parts.",
        CAPTURE_SCHEMA,
    )
    async def capture_frame(args: dict[str, Any]) -> dict[str, Any]:
        reason, view, detail = args["reason"], args["view"], args["detail"]

        def fail(code: str, message: str) -> dict[str, Any]:
            on_capture(CaptureEvent(reason, view, detail, error=code, message=message))
            return {"content": [{"type": "text", "text": f"No capture [{code}]: {message}"}], "is_error": True}

        if why := budget.blocked():
            return fail("throttled", f"Capture budget: {why}. Tell the user you can't look right now and why.")
        try:
            r = args.get("region")
            region = Region(r["x"], r["y"], r["w"], r["h"]) if r else None
        except (KeyError, TypeError, ValueError) as e:
            return fail("bad_region", str(e))
        try:
            cap = await camera.capture(view, detail, region, reason=reason)
        except CameraError as e:
            hint = {
                "no_frames": " The camera may still be reconnecting; tell the user it isn't ready yet.",
                "unknown_camera": " The phone camera isn't available right now; tell the user. It shows up when the phone "
                "is near the Mac and locked, with Wi-Fi, Bluetooth and Continuity Camera on; no app needs to run.",
            }.get(e.code, "")
            return fail(e.code, e.message + hint)

        budget.record()
        number = captures.add(cap)
        on_capture(CaptureEvent(reason, view, detail, capture=cap, number=number))
        turn_left, minute_left = budget.remaining()
        region_text = (
            f"region x={cap.region.x:.3f} y={cap.region.y:.3f} w={cap.region.w:.3f} h={cap.region.h:.3f}"
            if cap.region else "full frame"
        )
        info = (
            f"Capture {number}: {cap.camera.name}, {detail}, {region_text}, {cap.width}x{cap.height}px "
            f"from a {cap.source_size[0]}x{cap.source_size[1]} frame. "
            f"Captures left: {turn_left} this turn, {minute_left} this minute."
        )
        return {
            "content": [
                {"type": "image", "data": base64.b64encode(cap.image).decode(), "mimeType": cap.mime},
                {"type": "text", "text": info},
            ]
        }

    @tool(
        "annotate_capture",
        "Show the user one of your captures with boxes, circles, arrows or labels drawn on it, in a popover on "
        "their screen, to point something out or show where to do something. Costs no capture.",
        ANNOTATE_SCHEMA,
    )
    async def annotate_capture(args: dict[str, Any]) -> dict[str, Any]:
        number = args.get("capture") or captures.latest
        if number is None:
            return _error("No capture to annotate yet: take one with capture_frame first.")
        assert show_popover is not None  # only registered with one
        try:
            pid = show_popover("annotated-image", str(args.get("title", "")).strip(), {
                "capture": int(number),
                "annotations": args.get("annotations"),
                "caption": str(args.get("caption") or "").strip(),
            })
        except (TypeError, ValueError) as e:
            return _error(f"Not shown: {e}")
        return {"content": [{"type": "text", "text": f"Shown on the user's screen (popover {pid}, capture {number}). "
                             'They can close it themselves or say "dismiss".'}]}

    @tool(
        "show_steps",
        "Show instructions with more than one physical step (wiring several pins, a sequence of settings) as a "
        "group the user steps through on their screen: one marked-up capture per step, marking only that step, "
        "with earlier steps greyed. Steps can share a capture or each use their own (e.g. an inspect of the part "
        "a step is about). Costs no capture.",
        STEPS_SCHEMA,
    )
    async def show_steps(args: dict[str, Any]) -> dict[str, Any]:
        if captures.latest is None:
            return _error("No capture to mark up yet: take one with capture_frame first.")
        assert show_popover is not None
        try:
            steps = args.get("steps")
            pid = show_popover("steps", str(args.get("title", "")).strip(), {
                "steps": steps, "show_earlier": args.get("show_earlier", True) is not False,
            })
        except (TypeError, ValueError) as e:
            return _error(f"Not shown: {e}")
        n = len(steps)
        return {"content": [{"type": "text", "text":
            f"Shown on the user's screen as steps popover {pid}, at step 1 of {n}. In your reply, say briefly that "
            f"there are {n} steps on the screen and give step 1 only. Don't read the other steps: the screen reads "
            'each one out as the user moves to it by saying "next", "back" or "repeat" (or with its buttons). '
            "When the user writes to you while the steps are open, their message starts with the step on screen."}]}

    tools = [capture_frame] + ([annotate_capture, show_steps] if show_popover else [])
    return create_sdk_mcp_server(SERVER_NAME, tools=tools)


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}
