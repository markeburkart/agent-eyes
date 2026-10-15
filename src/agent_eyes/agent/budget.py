"""Hard limit on how often the agent may look, per user turn and per rolling minute."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class CaptureBudget:
    """Counts successful captures. `blocked()` says why the next one isn't allowed, or None."""

    def __init__(self, per_turn: int = 3, per_minute: int = 10, clock: Callable[[], float] = time.monotonic):
        self.per_turn = per_turn
        self.per_minute = per_minute
        self._clock = clock
        self._turn = 0
        self._recent: deque[float] = deque()

    def start_turn(self) -> None:
        self._turn = 0

    def blocked(self) -> str | None:
        self._expire()
        if self._turn >= self.per_turn:
            return f"you already used all {self.per_turn} captures allowed for this turn"
        if len(self._recent) >= self.per_minute:
            wait = 60 - (self._clock() - self._recent[0])
            return f"the limit of {self.per_minute} captures per minute is reached; the next one is allowed in {wait:.0f}s"
        return None

    def record(self) -> None:
        self._turn += 1
        self._recent.append(self._clock())

    def remaining(self) -> tuple[int, int]:
        """(left this turn, left this minute)."""
        self._expire()
        return self.per_turn - self._turn, self.per_minute - len(self._recent)

    def _expire(self) -> None:
        now = self._clock()
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
