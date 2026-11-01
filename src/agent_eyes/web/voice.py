"""Voice for the dashboard: wake word, dictation and replies read out, in the web server's process.

Spoken requests go to the LiveSession like typed ones. The page shows the voice state from
`summary()` big enough to read from the bench: off, starting, idle (waiting for the wake word),
dictating, answering (the agent is working on a request or reading its reply out) or live
(follow-ups need no wake word), with the seconds left before a live conversation closes, and a
button to close it (`end_conversation()`, like saying "ok thanks"). Each
change is also a session event, so the page's long poll picks it up at once. The same sounds as
in the terminal chat play.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..voice import (
    DEFAULT_WAKE_WORDS,
    Cancelled,
    Command,
    ConversationEnded,
    ConversationOpened,
    Focus,
    FollowUpHeard,
    Interrupted,
    VoiceError,
    VoiceHelper,
    WakeHeard,
    build_helper,
    chime,
    voice_commands,
)

if TYPE_CHECKING:
    from .live import LiveSession


@dataclass(frozen=True)
class VoiceOptions:
    wake_words: tuple[str, ...] = DEFAULT_WAKE_WORDS
    pause: float = 1.5
    follow_up: float = 30.0
    locale: str | None = None
    audio_file: str | None = None
    """Played instead of the mic, for testing (the readout is silent then)."""
    listening: bool = False
    """Voice commands on at startup."""
    speaking: bool = False
    """Replies read out at startup."""


class LiveVoice:
    """The dashboard's two voice toggles, like the chat's: voice commands (the mic is open, listening
    for the wake word) and speech (replies are read out). Lives on the backend's event loop."""

    def __init__(self, session: LiveSession, options: VoiceOptions):
        self.session = session
        self.options = options
        self.focus = Focus(options.follow_up)
        self.listening = False
        self.speaking = False
        self.dictating = False
        self.starting = False
        self.error: str | None = None
        self._helper: VoiceHelper | None = None
        self._stack = contextlib.AsyncExitStack()
        self._listen_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def set(self, *, listening: bool | None = None, speaking: bool | None = None) -> None:
        """Switches voice commands and/or speech on or off, starting voicesvc the first time either is on."""
        async with self._lock:
            listening = self.listening if listening is None else listening
            speaking = self.speaking if speaking is None else speaking
            try:
                if self._helper is None and (listening or speaking):
                    self.starting = True
                    self.session.push("voice")
                    self._helper = await self._start(mic=listening)
                    self._listen_task = asyncio.create_task(self._listen(self._helper))
                elif self._helper and listening != self.listening:
                    await self._helper.set_mic(listening)
                if self._helper and self.speaking and not speaking:
                    await self._helper.stop_speaking()
            except VoiceError as e:
                self.error = f"voice error [{e.code}]: {e.message}"
                self.session.push("status", text=self.error, error=True)
                return
            finally:
                self.starting = False
            if self.listening and not listening:
                self.focus.end()
                self.dictating = False
            changed = (listening, speaking) != (self.listening, self.speaking)
            self.listening, self.speaking, self.error = listening, speaking, None
            self.session.push("voice", **({"text": self.status()} if changed else {}))

    async def _start(self, mic: bool) -> VoiceHelper:
        if "AGENT_EYES_VOICESVC" not in os.environ:
            await asyncio.to_thread(build_helper)
        o = self.options
        return await self._stack.enter_async_context(VoiceHelper(
            locale=o.locale, context=o.wake_words, audio_file=o.audio_file,
            speech_volume=0.0 if o.audio_file else 1.0, mic=mic,
        ))

    def status(self) -> str:
        if self.listening:
            listening = f'voice commands on (say "{self.options.wake_words[0]}", then your request)'
        else:
            listening = "voice commands off"
        return f"{listening}, speech {'on' if self.speaking else 'off'}"

    def state(self) -> str:
        """off | starting | idle | dictating | answering | live"""
        if self.starting:
            return "starting"
        if not self.listening:
            return "off"
        if self.session.state == "thinking" or (self._helper and self._helper.echo.speaking):
            return "answering"
        if self.dictating:
            return "dictating"
        return "live" if self.focus.active else "idle"

    def summary(self) -> dict:
        return {
            "state": self.state(),
            "listening": self.listening,
            "speaking": self.speaking,
            "live": self.listening and self.focus.active,
            "left_s": self.focus.left if self.listening else None,
            "follow_up_s": self.focus.seconds,
            "wake_word": self.options.wake_words[0],
            "error": self.error,
        }

    # ---- hooks for the session's turns -----------------------------------------

    def turn_started(self) -> None:
        self.focus.busy = True  # the agent's thinking time doesn't count against the conversation

    def turn_ended(self) -> None:
        self.focus.busy = False
        self.focus.extend()

    def end_conversation(self) -> None:
        """Closes a live conversation and drops a request being dictated, like saying "ok thanks".
        voice_commands stops a readout and yields ConversationEnded."""
        if self.listening and (self.focus.active or self.dictating):
            self.focus.close("closed from the dashboard")
            self.dictating = False
            self.session.push("voice")

    async def say(self, text: str) -> None:
        if self.speaking and self._helper:
            await self._helper.speak(text)

    async def hush(self) -> None:
        """Cuts off a readout in progress."""
        if self._helper:
            await self._helper.stop_speaking()

    async def close(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
        await self._stack.aclose()

    # ---- listening -------------------------------------------------------------

    async def _listen(self, helper: VoiceHelper) -> None:
        o = self.options
        push = self.session.push
        try:
            async for event in voice_commands(helper, o.wake_words, focus=self.focus, pause=o.pause):
                if isinstance(event, (WakeHeard, FollowUpHeard)):
                    if isinstance(event, WakeHeard):
                        chime("Tink")
                    self.dictating = True
                    push("voice")
                    continue
                self.dictating = False
                if isinstance(event, Command):
                    if not self.listening:
                        continue  # cut off by switching voice commands off
                    chime("Pop")
                    try:
                        await self.session.ask(event.text, voice=True)
                    except RuntimeError as e:
                        chime("Bottle")
                        push("status", text=f'not sent ({e}), please repeat: "{event.text}"', error=True)
                elif isinstance(event, ConversationOpened):
                    push("voice", text=f"conversation live: follow up without the wake word; closes after "
                                       f"{self.focus.seconds:g} s of quiet or when you say \"ok thanks\" or \"that's all\"")
                elif isinstance(event, Interrupted):
                    push("voice", text=f'readout stopped: you said "{event.word}"')
                elif isinstance(event, ConversationEnded):
                    if self.listening:  # otherwise switching voice commands off closed it
                        chime("Submarine")
                        push("voice", text=f'conversation closed: {event.reason}; say "{o.wake_words[0]}" to start again')
                elif isinstance(event, Cancelled):
                    chime("Bottle")
                    push("voice", text=f"{event.reason}{': ' + event.text if event.text else ''}")
        except VoiceError as e:
            # voicesvc is gone: switch voice off; switching it on again starts a new one.
            self._helper, self._listen_task = None, None
            await self._stack.aclose()
            self._stack = contextlib.AsyncExitStack()
            self.listening = self.speaking = self.dictating = False
            self.focus.end()
            self.error = f"voice error [{e.code}]: {e.message}"
            push("status", text=self.error, error=True)
