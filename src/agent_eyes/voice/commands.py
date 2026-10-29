"""Wake word + dictation: turns a running transcript into prompts for the agent.

Say the wake word, then the request ("hey eyes, is the red wire on pin five?"). The request
ends when you pause. Saying the wake word alone and then the request also works. Ending with
"never mind" (or saying only "cancel") drops it.

After a request the conversation stays open: follow-ups ("like this?") need no wake word until
nobody has said anything for a while (`Focus`, 30 s by default, held while the agent is
answering). "That's all" or "stop listening" closes it straight away.

Replies are read out with the mic still on. While the assistant is speaking, dictation pauses
and its own words are filtered out of what the mic hears (`Echo`); saying "stop" or "ok" cuts
the readout short.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from .echo import Echo
from .helper import Transcript

DEFAULT_WAKE_WORDS = ("hey eyes", "hey ice", "hi eyes")
CANCEL_PHRASES = ("never mind", "nevermind", "cancel")
END_PHRASES = ("that's all", "that is all", "stop listening", "goodbye")
INTERRUPT_WORDS = ("stop", "ok", "skip")
_FILLER = " ,.;:!?-"
_HESITATIONS = {"um", "uh", "hmm", "mm", "ah", "er", "oh"}


class TranscriptSource(Protocol):
    echo: Echo

    async def next(self, timeout: float | None = None) -> Transcript | None: ...
    async def finalize(self) -> None: ...
    async def stop_speaking(self) -> None: ...


class Focus:
    """Whether the conversation is open, so speech without the wake word counts as a request.

    `start()` opens it (after each spoken request); it closes `seconds` after the last `start()`
    or `extend()`, or with `end()`. While `busy` (the agent is answering) it doesn't time out."""

    def __init__(self, seconds: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self.busy = False
        self._clock = clock
        self._open = False
        self._until = -math.inf

    def start(self) -> None:
        self._open = True
        self._until = self._clock() + self.seconds

    def extend(self) -> None:
        """Restarts the timeout of an open conversation (e.g. after a reply); never reopens a closed one."""
        if self.active:
            self.start()

    def end(self) -> None:
        self._open = False

    @property
    def active(self) -> bool:
        if self._open and not self.busy and self._clock() >= self._until:
            self._open = False
        return self._open

    @property
    def left(self) -> float | None:
        """Seconds until the open conversation times out (all of them while busy), or None if it's closed."""
        if not self.active:
            return None
        return self.seconds if self.busy else max(0.0, self._until - self._clock())


@dataclass(frozen=True)
class WakeHeard:
    """The wake word was heard; dictation has started."""


@dataclass(frozen=True)
class FollowUpHeard:
    """Speech in a live conversation (no wake word needed); dictation has started."""


@dataclass(frozen=True)
class Command:
    text: str


@dataclass(frozen=True)
class Cancelled:
    reason: str
    text: str = ""


@dataclass(frozen=True)
class ConversationOpened:
    """Follow-ups no longer need the wake word (until ConversationEnded)."""


@dataclass(frozen=True)
class ConversationEnded:
    """The conversation closed; the wake word is needed again."""

    reason: str


@dataclass(frozen=True)
class Interrupted:
    """The user cut the readout short."""

    word: str


VoiceEvent = WakeHeard | FollowUpHeard | Command | Cancelled | ConversationOpened | ConversationEnded | Interrupted


def _words(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"[a-z0-9']+", text.lower()))


def after_wake_word(text: str, wake_words: tuple[str, ...]) -> str | None:
    """What was said after the last wake word in `text` (ignoring case and punctuation), or None if there is none."""
    words = _words(text)
    spoken = [m.group() for m in words]
    end = None
    for phrase in wake_words:
        target = phrase.lower().split()
        for i in range(len(spoken) - len(target) + 1):
            if spoken[i : i + len(target)] == target and (end is None or words[i + len(target) - 1].end() > end):
                end = words[i + len(target) - 1].end()
    return None if end is None else text[end:].strip(_FILLER)


def _ends_with(command: str, phrases: tuple[str, ...]) -> bool:
    spoken = " ".join(m.group() for m in _words(command))
    return any(spoken == p or spoken.endswith(" " + p) for p in phrases)


def is_cancel(command: str) -> bool:
    spoken = " ".join(m.group() for m in _words(command))
    return spoken == "cancel" or _ends_with(command, tuple(p for p in CANCEL_PHRASES if p != "cancel"))


def is_end(command: str) -> bool:
    return _ends_with(command, END_PHRASES)


def is_hesitation(text: str) -> bool:
    """Nothing but "um", "uh" and the like: not worth sending to the agent."""
    return all(m.group() in _HESITATIONS for m in _words(text))


async def voice_commands(
    source: TranscriptSource,
    wake_words: tuple[str, ...] = DEFAULT_WAKE_WORDS,
    *,
    focus: Focus | None = None,
    pause: float = 1.5,
    wait_for_command: float = 8.0,
    finalize_wait: float = 2.0,
) -> AsyncIterator[VoiceEvent]:
    """Yields WakeHeard when the wake word is spoken, then a Command once the speaker pauses for
    `pause` seconds, or Cancelled. Speech without the wake word is ignored unless `focus` is active
    (then it yields FollowUpHeard and dictation starts);
    yields ConversationOpened and ConversationEnded as focus comes and goes. While `source.echo` is
    active only interrupt words count; yields Interrupted after stopping the readout."""
    focus = focus or Focus(0)
    settled = ""  # final transcript text since the last command
    volatile = ""  # the recognizer's current in-progress guess, following `settled`
    dictating = False  # collecting a request (after the wake word, or any speech while focused)
    finalize_sent = False
    was_focused = False
    in_echo = False  # the assistant is (or just was) speaking: listen only for interrupt words
    interrupted = False
    echo = source.echo

    def request_text() -> str:
        heard = settled + volatile
        command = after_wake_word(heard, wake_words)
        return heard.strip(_FILLER) if command is None else command

    while True:
        if in_echo and not echo.active:
            # The readout (and its delayed transcript) is over; start listening afresh.
            settled, volatile, in_echo, interrupted = "", "", False, False
        if in_echo:
            focus.extend()  # the follow-up window starts once the reply has been read out
        if not dictating:
            if was_focused and not focus.active:
                yield ConversationEnded("no one spoke for a while")
            elif not was_focused and focus.active:
                yield ConversationOpened()
            was_focused = focus.active
        command = request_text() if dictating else None
        if in_echo or echo.active:
            timeout = 0.5  # poll, to notice when the readout ends
        elif not dictating:
            # Poll while focused, so the conversation can close in silence.
            timeout = 1.0 if focus.active else None
        elif not command:
            timeout = wait_for_command
        elif volatile and finalize_sent:
            timeout = finalize_wait
        else:
            timeout = pause

        t = await source.next(timeout)
        if t is None:
            if not dictating or in_echo or echo.active:
                continue
            # Quiet for `timeout` seconds while dictating.
            if command and volatile and not finalize_sent:
                # Settle the in-progress guess first: final text is more accurate.
                await source.finalize()
                finalize_sent = True
                continue
            settled, volatile, dictating, finalize_sent = "", "", False, False
            if not command:
                yield Cancelled("no request heard after the wake word")
            elif is_end(command):
                if was_focused:
                    yield ConversationEnded("you closed it")
                focus.end()
                was_focused = False
            elif is_cancel(command):
                focus.extend()
                yield Cancelled("cancelled", command)
            elif not is_hesitation(command):
                focus.start()
                yield Command(command)
            continue

        if echo.active and not in_echo:
            in_echo = True
            if dictating:
                dictating = False
                if (cut := request_text()) and not is_hesitation(cut):
                    yield Cancelled("talked over by the reply, please repeat", cut)
            settled, volatile, finalize_sent = "", "", False
        if t.final:
            settled += t.text
            volatile = ""
        else:
            volatile = t.text
            finalize_sent = False
        if in_echo:
            said = set(echo.leftover(settled + volatile))
            if not said:
                echo.heard_echo()
            if not interrupted and echo.speaking:
                if word := next((w for w in INTERRUPT_WORDS if w in said), None):
                    interrupted = True
                    await source.stop_speaking()
                    yield Interrupted(word)
            continue
        if not dictating:
            if after_wake_word(settled + volatile, wake_words) is not None:
                dictating = True
                yield WakeHeard()
            elif focus.active and (settled + volatile).strip(_FILLER):
                dictating = True
                yield FollowUpHeard()
            elif t.final:
                settled = ""  # chatter without the wake word
