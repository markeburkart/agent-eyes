from .base import CameraError, CameraInfo, CameraSource, Capture, Detail, Region
from .continuity import ContinuityCameraSource

__all__ = [
    "CameraError",
    "CameraInfo",
    "CameraSource",
    "Capture",
    "ContinuityCameraSource",
    "Detail",
    "Region",
    "make_camera_source",
]


def make_camera_source(backend: str = "continuity", **kwargs) -> CameraSource:
    """Single place to choose the capture backend; add new backends here."""
    if backend == "continuity":
        return ContinuityCameraSource(**kwargs)
    raise ValueError(f"unknown camera backend {backend!r}")
