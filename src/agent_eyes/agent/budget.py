"""Hard limit on how often the agent may look, per user turn and per rolling minute."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class CaptureBudget:
    """Counts successful captures. `blocked()` says why the next one isn't allowed, or None.

    Limits and `paused` can be changed at any time (the dashboard does); they apply to the next capture.
    """

    def __init__(self, per_turn: int = 3, per_minute: int = 10, clock: Callable[[], float] = time.monotonic):
        self.per_turn = per_turn
        self.per_minute = per_minute
        self.paused = False
        """The user has switched the camera off for the agent."""
        self._clock = clock
        self._turn = 0
        self._recent: deque[float] = deque()

    def start_turn(self) -> None:
        self._turn = 0

    def blocked(self) -> str | None:
        self._expire()
        if self.paused:
            return "the user has switched the camera off for now"
        if self._turn >= self.per_turn:
            return f"you already used all {self.per_turn} captures allowed for this turn"
        if len(self._recent) >= self.per_minute:
            return f"the limit of {self.per_minute} captures per minute is reached; the next one is allowed in {self.next_in():.0f}s"
        return None

    def record(self) -> None:
        self._turn += 1
        self._recent.append(self._clock())

    def remaining(self) -> tuple[int, int]:
        """(left this turn, left this minute)."""
        self._expire()
        return max(0, self.per_turn - self._turn), max(0, self.per_minute - len(self._recent))

    def next_in(self) -> float:
        """Seconds until the per-minute limit frees a capture (0 if one is free now)."""
        self._expire()
        if len(self._recent) < self.per_minute:
            return 0.0
        if self.per_minute <= 0:
            return 60.0
        # After lowering the limit several old captures may have to expire first.
        return 60 - (self._clock() - self._recent[len(self._recent) - self.per_minute])

    def state(self) -> dict:
        self._expire()
        return {
            "per_turn": self.per_turn,
            "per_minute": self.per_minute,
            "paused": self.paused,
            "used_turn": self._turn,
            "used_minute": len(self._recent),
            "next_in_s": round(self.next_in(), 1),
            "blocked": self.blocked(),
        }

    def _expire(self) -> None:
        now = self._clock()
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
