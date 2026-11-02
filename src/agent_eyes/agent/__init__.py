"""The Agent Eyes agent: Claude Agent SDK session, camera and annotation tools, capture budget."""

from .budget import CaptureBudget
from .session import DEFAULT_MODEL, EyesAgent, TurnResult
from .tools import CaptureEvent, CaptureLog

__all__ = ["DEFAULT_MODEL", "CaptureBudget", "CaptureEvent", "CaptureLog", "EyesAgent", "TurnResult"]
