"""The assistant's own voice, as the microphone hears it.

The mic stays on while replies are read out. Heard words that match what has been said aloud so
far, in order, are the assistant's echo; anything left over is the user (e.g. "stop").
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable

# Words the speech recognizer and the reply text may spell differently.
_SAME: dict[str, str] = {"okay": "ok", "o.k": "ok"}


def spoken_words(text: str) -> list[str]:
    words: list[str] = re.findall(r"[a-z0-9']+(?:\.[a-z])?", text.lower())
    return [_SAME.get(w) or w for w in words]


class Echo:
    """Tracks what is being read out. Active while speaking and for `grace` seconds after (longer
    while the tail of the readout is still being heard), since the recognizer runs a little late."""

    def __init__(self, grace: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self.grace = grace
        self.words: list[str] = []  # everything said aloud in this readout, up to the current word
        self._pending = 0  # utterances queued or playing
        self._clock = clock
        self._ended = -math.inf

    def begin(self) -> None:
        """An utterance was queued."""
        if not self.active:
            self.words = []
        self._pending += 1

    def spoke(self, words: list[str]) -> None:
        self.words.extend(words)

    def done(self) -> None:
        """An utterance finished or was cut off."""
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._ended = self._clock()

    def heard_echo(self) -> None:
        """The mic just heard nothing but the readout: its delayed tail is still coming in, so stay active."""
        if self._pending == 0 and self.active:
            self._ended = self._clock()

    @property
    def speaking(self) -> bool:
        return self._pending > 0

    @property
    def active(self) -> bool:
        return self._pending > 0 or self._clock() - self._ended < self.grace

    def leftover(self, heard: str) -> list[str]:
        """The heard words that aren't the readout. Each heard word is matched to the next
        occurrence, after the previous match, of the same word among those already said aloud."""
        left = []
        at = 0
        for word in spoken_words(heard):
            try:
                at = self.words.index(word, at) + 1
            except ValueError:
                left.append(word)
        return left
