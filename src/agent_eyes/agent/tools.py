"""In-process MCP tools the agent uses to look at the bench."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..camera import CameraError, CameraSource, Capture, Region
from .budget import CaptureBudget

SERVER_NAME = "eyes"
CAPTURE_TOOL = f"mcp__{SERVER_NAME}__capture_frame"

CAPTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "Why you need to look, in a few words. Shown to the user."},
        "view": {
            "type": "string",
            "description": 'Which camera: "front" (the main camera, looking down at the work area) or "desk" (Desk View). '
            "A camera id also works.",
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


@dataclass(frozen=True)
class CaptureEvent:
    """One capture_frame call, for whoever displays them (terminal now, dashboard later)."""

    reason: str
    view: str
    detail: str
    capture: Capture | None = None
    error: str | None = None
    """Error code ("throttled", "no_frames", ...) when nothing was captured."""
    message: str = ""


def capture_server(camera: CameraSource, budget: CaptureBudget, on_capture: Callable[[CaptureEvent], None]):
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
            hint = " The camera may still be reconnecting; tell the user it isn't ready yet." if e.code == "no_frames" else ""
            return fail(e.code, e.message + hint)

        budget.record()
        on_capture(CaptureEvent(reason, view, detail, capture=cap))
        turn_left, minute_left = budget.remaining()
        region_text = (
            f"region x={cap.region.x:.3f} y={cap.region.y:.3f} w={cap.region.w:.3f} h={cap.region.h:.3f}"
            if cap.region else "full frame"
        )
        info = (
            f"{cap.camera.name}, {detail}, {region_text}, {cap.width}x{cap.height}px "
            f"from a {cap.source_size[0]}x{cap.source_size[1]} frame. "
            f"Captures left: {turn_left} this turn, {minute_left} this minute."
        )
        return {
            "content": [
                {"type": "image", "data": base64.b64encode(cap.image).decode(), "mimeType": cap.mime},
                {"type": "text", "text": info},
            ]
        }

    return create_sdk_mcp_server(SERVER_NAME, tools=[capture_frame])
