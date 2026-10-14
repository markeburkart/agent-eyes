"""Camera abstraction used by the agent.

The agent only sees `CameraSource`. Each backend decides how to fulfil a request: the
Continuity Camera backend crops video frames; a USB microscope or a phone-side app could
serve `inspect` from a different device or a high-resolution still without the agent changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

Detail = Literal["overview", "inspect"]


class CameraError(Exception):
    """A capture failed. `code` is a stable identifier the agent can be told about."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Region:
    """Normalized rectangle (0..1), origin top-left, relative to the camera's full frame."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if not (self.w > 0 and self.h > 0):
            raise ValueError("region width and height must be positive")
        for name, v in (("x", self.x), ("y", self.y), ("w", self.w), ("h", self.h)):
            if not 0 <= v <= 1:
                raise ValueError(f"region {name}={v} is outside 0..1")


@dataclass(frozen=True)
class CameraInfo:
    id: str
    name: str
    kind: str
    """continuity | desk_view | external | built_in (backend-specific values allowed)."""
    role: str | None = None
    """Stable alias such as "front" or "desk"."""
    connected: bool = True
    streaming: bool = False
    frame_size: tuple[int, int] | None = None


@dataclass(frozen=True)
class Capture:
    image: bytes
    mime: str
    width: int
    height: int
    camera: CameraInfo
    detail: Detail
    reason: str
    region: Region | None
    """Region actually captured (after clamping), or None for the full frame."""
    source_size: tuple[int, int]
    captured_at: datetime
    sharpness: float | None = None
    extra: dict = field(default_factory=dict)
    """Backend diagnostics (candidate sharpness scores, frame age, ...)."""


class CameraSource(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def list_cameras(self) -> list[CameraInfo]: ...

    async def capture(
        self,
        view: str,
        detail: Detail = "overview",
        region: Region | None = None,
        reason: str = "",
    ) -> Capture:
        """Capture one image.

        view: a role ("front", "desk") or a backend camera id.
        detail: "overview" for a cheap whole-scene look, "inspect" for maximum detail of `region`.
        Raises CameraError on failure.
        """
        ...
