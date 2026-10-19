"""The Agent Eyes agent: Claude Agent SDK session, camera tool and capture budget."""

from .budget import CaptureBudget
from .session import DEFAULT_MODEL, EyesAgent, TurnResult
from .tools import CaptureEvent

__all__ = ["DEFAULT_MODEL", "CaptureBudget", "CaptureEvent", "EyesAgent", "TurnResult"]
