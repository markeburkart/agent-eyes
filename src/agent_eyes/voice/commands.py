"""Wake word + dictation: turns a running transcript into prompts for the agent.

Say the wake word, then the request ("hey eyes, is the red wire on pin five?"). The request
ends when you pause. Saying the wake word alone and then the request also works. Ending with
"never mind" (or saying only "cancel") drops it.

After a request the conversation stays open: follow-ups ("like this?") need no wake word until
nobody has said anything for a while (`Focus`, 30 s by default, held while the agent is
answering). "Ok thanks", "that's all" or "stop listening" closes it straight away, as does
`Focus.close()` (e.g. a button).

Replies are read out with the mic still on. While the assistant is speaking, dictation pauses
and its own words are filtered out of what the mic hears (`Echo`); saying "stop" or "ok" cuts
the readout short, and "ok thanks" (or any other closing phrase) also closes the conversation.

Spoken actions: a request that is nothing but one of an action's phrases ("hey eyes, new
conversation", "dismiss") yields Action(name) instead of a Command, for the app to carry out
rather than send to the agent. ACTIONS lists them; voice_commands only knows the ones it's given
(`actions`), so each app picks those it can handle. Like any request, an action needs the wake word
unless the conversation is live, and isn't heard while a reply is being read out.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .echo import Echo
from .helper import Transcript

# ---- what can be said -----------------------------------------------------------
# Phrases are lowercase words; what's heard is matched ignoring case and punctuation.

DEFAULT_WAKE_WORDS = ("hey eyes", "hey ice", "hi eyes")

# Conversation control, built into voice_commands.
CANCEL_PHRASES = ("never mind", "nevermind", "cancel")  # a request ending with one is dropped
END_PHRASES = ("that's all", "that is all", "stop listening", "goodbye",  # a request ending with one closes the
               "ok thanks", "okay thanks", "ok thank you", "okay thank you")  # conversation (also during a readout)
INTERRUPT_WORDS = ("stop", "ok", "skip")  # cut a readout short

# Spoken actions, by name: a request that is exactly one of the phrases yields Action(name).
# To add a command, add an entry here and a handler with the same name where voice_commands runs
# (the dashboard's LiveVoice.actions); to change what's said for one, edit its phrases.
ACTIONS: dict[str, tuple[str, ...]] = {
    # The agent forgets the conversation so far.
    "new_conversation": ("new conversation", "start over", "clear context"),
    # Close the popover on top (the one opened or clicked last).
    "dismiss": ("dismiss", "dismissed", "dismiss it", "dismiss that", "dismiss this"),
    # Close every popover. "Dismissal" is how the recognizer can hear "dismiss all".
    "dismiss_all": ("dismiss all", "dismissal", "dismiss all of them", "dismiss them all", "dismiss them",
                    "dismiss everything"),
}
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
    or `extend()`, or with `end()` or `close()`. While `busy` (the agent is answering) it doesn't time out."""

    def __init__(self, seconds: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self.busy = False
        self._clock = clock
        self._open = False
        self._until = -math.inf
        self.closing: str | None = None
        """Why `close()` was called, until voice_commands has acted on it."""

    def start(self) -> None:
        self._open = True
        self._until = self._clock() + self.seconds

    def extend(self) -> None:
        """Restarts the timeout of an open conversation (e.g. after a reply); never reopens a closed one."""
        if self.active:
            self.start()

    def end(self) -> None:
        self._open = False

    def close(self, reason: str) -> None:
        """Closes the conversation from outside voice_commands (e.g. a button), as if the closing
        phrase had been said: it drops a request being dictated, stops a readout and yields ConversationEnded."""
        self._open = False
        self.closing = reason

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
class Action:
    """The request was one of the phrases of action `name` (see ACTIONS), for the app to carry out."""

    name: str


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


VoiceEvent = (WakeHeard | FollowUpHeard | Command | Action | Cancelled | ConversationOpened | ConversationEnded
              | Interrupted)


def _words(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"[a-z0-9']+", text.lower()))


def _wake_end(text: str, wake_words: tuple[str, ...]) -> int | None:
    """How many words of `text` there are up to the end of the last wake word, or None if there is none."""
    spoken = [m.group() for m in _words(text)]
    end = None
    for phrase in wake_words:
        target = phrase.lower().split()
        for i in range(len(spoken) - len(target) + 1):
            if spoken[i : i + len(target)] == target and (end is None or i + len(target) > end):
                end = i + len(target)
    return end


def _after_words(text: str, n: int) -> str:
    """`text` without its first `n` words."""
    words = _words(text)
    return text[words[n - 1].end():].strip(_FILLER) if n else text.strip(_FILLER)


def after_wake_word(text: str, wake_words: tuple[str, ...]) -> str | None:
    """What was said after the last wake word in `text` (ignoring case and punctuation), or None if there is none."""
    end = _wake_end(text, wake_words)
    return None if end is None else _after_words(text, end)


def _ends_with(command: str, phrases: tuple[str, ...]) -> bool:
    spoken = " ".join(m.group() for m in _words(command))
    return any(spoken == p or spoken.endswith(" " + p) for p in phrases)


def is_cancel(command: str) -> bool:
    spoken = " ".join(m.group() for m in _words(command))
    return spoken == "cancel" or _ends_with(command, tuple(p for p in CANCEL_PHRASES if p != "cancel"))


def is_end(command: str) -> bool:
    return _ends_with(command, END_PHRASES)


def action_for(command: str, actions: Mapping[str, tuple[str, ...]] = ACTIONS) -> str | None:
    """The action whose phrase is the whole request, if any ("let's start over with pin five" is a request)."""
    spoken = " ".join(m.group() for m in _words(command))
    return next((name for name, phrases in actions.items() if spoken in phrases), None)


def is_hesitation(text: str) -> bool:
    """Nothing but "um", "uh" and the like: not worth sending to the agent."""
    return all(m.group() in _HESITATIONS for m in _words(text))


async def voice_commands(
    source: TranscriptSource,
    wake_words: tuple[str, ...] = DEFAULT_WAKE_WORDS,
    *,
    focus: Focus | None = None,
    actions: Mapping[str, tuple[str, ...]] | None = None,
    pause: float = 1.5,
    wait_for_command: float = 8.0,
    finalize_wait: float = 2.0,
) -> AsyncIterator[VoiceEvent]:
    """Yields WakeHeard when the wake word is spoken, then a Command once the speaker pauses for
    `pause` seconds, or Cancelled. Speech without the wake word is ignored unless `focus` is active
    (then it yields FollowUpHeard and dictation starts);
    yields ConversationOpened and ConversationEnded as focus comes and goes, and Action for a request
    that is one of the phrases in `actions` (name -> phrases, e.g. from ACTIONS). While `source.echo` is
    active only interrupt words count; yields Interrupted after stopping the readout."""
    focus = focus or Focus(0)
    settled = ""  # final transcript text since the last command
    volatile = ""  # the recognizer's current in-progress guess, following `settled`
    dictating = False  # collecting a request (after the wake word, or any speech while focused)
    finalize_sent = False
    was_focused = False
    in_echo = False  # the assistant is (or just was) speaking: listen only for interrupt words
    interrupted = False
    closed = False  # a closing phrase was heard during this readout
    woke_at: int | None = None  # words up to the end of the wake word that started this dictation
    echo = source.echo

    def request_text() -> str:
        heard = settled + volatile
        command = after_wake_word(heard, wake_words)
        if command is None and woke_at and len(_words(heard)) > woke_at:
            # The recognizer revised the wake word away ("hey eyes" became "hey, guys"): drop it anyway.
            return _after_words(heard, woke_at)
        return heard.strip(_FILLER) if command is None else command

    while True:
        if in_echo and not echo.active:
            # The readout (and its delayed transcript) is over; start listening afresh.
            settled, volatile, in_echo, interrupted, closed = "", "", False, False, False
        if focus.closing is not None:
            reason, focus.closing = focus.closing, None
            if echo.speaking:
                await source.stop_speaking()
                interrupted = True
            if was_focused or dictating:
                yield ConversationEnded(reason)
            settled, volatile, dictating, finalize_sent, was_focused = "", "", False, False, False
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
        if focus.closing is not None:
            continue  # closed while waiting: drop whatever was heard
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
            elif name := action_for(command, actions or {}):
                focus.start()
                yield Action(name)
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
            left = echo.leftover(settled + volatile)
            said = set(left)
            if not said:
                echo.heard_echo()
            if not closed and is_end(" ".join(left)):
                # E.g. "ok thanks": the "ok" may already have stopped the readout; now close too.
                closed = True
                if not interrupted and echo.speaking:
                    interrupted = True
                    await source.stop_speaking()
                    if not was_focused:
                        yield Interrupted(" ".join(left))
                if was_focused:
                    yield ConversationEnded("you closed it")
                focus.end()
                was_focused = False
                continue
            if not interrupted and echo.speaking:
                if word := next((w for w in INTERRUPT_WORDS if w in said), None):
                    interrupted = True
                    await source.stop_speaking()
                    yield Interrupted(word)
            continue
        if not dictating:
            if (woke_at := _wake_end(settled + volatile, wake_words)) is not None:
                dictating = True
                yield WakeHeard()
            elif focus.active and (settled + volatile).strip(_FILLER):
                dictating = True
                yield FollowUpHeard()
            elif t.final:
                settled = ""  # chatter without the wake word
