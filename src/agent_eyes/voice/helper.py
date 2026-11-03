"""Transcript source and speech output backed by the Swift `voicesvc` helper (on-device SpeechAnalyzer)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .echo import Echo, spoken_words

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BINARY = REPO_ROOT / "voice-service" / ".build" / "release" / "voicesvc"

# Speaking rates, on AVSpeechUtterance's 0..1 scale: each "slow down" multiplies by the step.
DEFAULT_RATE = 0.5
SLOW_DOWN_STEP = 0.8
MIN_RATE = 0.2


class VoiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Voice:
    """A speaking voice installed on the Mac, as voicesvc reports it."""

    identifier: str
    name: str
    """As the system names it, e.g. "Ava (Premium)"."""
    language: str
    quality: str
    """premium | enhanced | compact"""
    natural: bool = True
    """False for novelty voices (Bubbles, Zarvox, ...) and the robotic Eloquence ones (Eddy, Grandma, ...)."""

    @classmethod
    def of(cls, d: dict) -> Voice:
        return cls(d["identifier"], d["name"], d["language"], d["quality"], d.get("natural", True))

    @property
    def short_name(self) -> str:
        """"Ava" for "Ava (Premium)"."""
        return self.name.split(" (", 1)[0]

    def label(self, locale: str | None = None) -> str:
        """"Ava — Premium", with the region when it isn't `locale`'s: "Daniel — Compact · en-GB"."""
        region = f" · {self.language}" if locale and self.language != locale else ""
        return f"{self.short_name} — {self.quality.capitalize()}{region}"


@dataclass(frozen=True)
class Transcript:
    text: str
    final: bool
    """False: an in-progress guess that the next Transcript replaces. True: settled text; later ones follow it."""


def build_helper() -> None:
    """Builds (or incrementally rebuilds) the default voicesvc binary."""
    cmd = ["swift", "build", "-c", "release", "--package-path", str(REPO_ROOT / "voice-service")]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise VoiceError("helper_build_failed", "swift not found; install the Xcode command line tools")
    if result.returncode != 0:
        raise VoiceError("helper_build_failed", f"{' '.join(cmd)} failed:\n{result.stdout}{result.stderr}")


def helper_path(binary: Path | str | None = None) -> Path:
    return Path(binary or os.environ.get("AGENT_EYES_VOICESVC", DEFAULT_BINARY))


def list_voices(*, binary: Path | str | None = None, locale: str | None = None,
                voice: str | None = None) -> tuple[str, list[Voice], Voice | None]:
    """The locale voicesvc would use, its language's installed voices, best first, and the one it would speak
    with (`voice` if installed, else its default), without starting it. Needs the helper built."""
    cmd = [str(helper_path(binary)), "--list-voices"]
    if locale:
        cmd += ["--locale", locale]
    if voice:
        cmd += ["--voice", voice]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        event = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise VoiceError("voices_unavailable", f"could not list the voices: {e}")
    return event["locale"], [Voice.of(v) for v in event["voices"]], event["voice"] and Voice.of(event["voice"])


def chime(name: str) -> None:
    """Plays a short system sound without waiting for it: the user is looking at the bench, not the screen."""
    subprocess.Popen(["afplay", f"/System/Library/Sounds/{name}.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class VoiceHelper:
    """Spawns voicesvc, reads what it hears and speaks for the assistant. Use as an async context manager.

    `echo` follows what is being said aloud, so listeners can tell the assistant's voice from the user's."""

    def __init__(
        self,
        *,
        binary: Path | str | None = None,
        locale: str | None = None,
        context: tuple[str, ...] = (),
        audio_file: str | None = None,
        speech_volume: float = 1.0,
        mic: bool = True,
        voice: str | None = None,
        startup_timeout: float = 120.0,
    ):
        """`context` biases recognition (pass the wake words); `audio_file` replaces the mic, for testing.
        `mic=False` starts with the microphone closed (speaking only). `voice` (an identifier or name)
        speaks instead of the best installed voice for the locale. The startup timeout covers a
        one-time speech model download."""
        self.echo = Echo()
        self.mic_on = False
        self.rate = DEFAULT_RATE
        self.locale: str | None = None
        """The locale voicesvc settled on (e.g. en-US), once started."""
        self.voices: list[Voice] = []
        """Installed voices for the locale's language, best first, once started."""
        self.voice: Voice | None = None
        """The voice replies are read out with, once started."""
        self._utterances: dict[int, int] = {}  # id -> words of it already added to echo
        self._texts: dict[int, str] = {}  # id -> its text, until it's spoken
        self._reached: dict[int, int] = {}  # id -> characters of its text read out so far (up to the current word)
        self._next_id = 0
        self._binary = helper_path(binary)
        self._args: list[str] = []
        if locale:
            self._args += ["--locale", locale]
        if context:
            self._args += ["--context", ",".join(context)]
        if audio_file:
            self._args += ["--file", audio_file]
        self._args += ["--speech-volume", str(speech_volume)]
        if not mic:
            self._args.append("--mic-off")
        if voice:
            self._args += ["--voice", voice]
        self._startup_timeout = startup_timeout
        self._proc: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> VoiceHelper:
        if not self._binary.exists():
            raise VoiceError(
                "helper_missing",
                f"voicesvc not found at {self._binary}. Build it with: swift build -c release --package-path voice-service",
            )
        # stdin stays open for commands; closing it tells the helper to exit.
        self._proc = await asyncio.create_subprocess_exec(
            str(self._binary), *self._args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=sys.stderr,
        )
        try:
            async with asyncio.timeout(self._startup_timeout):
                while (event := await self._read_event())["event"] != "ready":
                    if event["event"] == "status":
                        print(f"voice: {event['message']}", file=sys.stderr, flush=True)
                self.locale = event["locale"]
                self.voices = [Voice.of(v) for v in event.get("voices", [])]
                self.voice = event.get("voice") and Voice.of(event["voice"])
        except TimeoutError:
            await self.close()
            raise VoiceError("helper_timeout", "voicesvc did not become ready (is a microphone permission prompt waiting?)")
        except VoiceError:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        assert self._proc.stdin
        self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), 5)
        except TimeoutError:
            self._proc.kill()

    async def next(self, timeout: float | None = None) -> Transcript | None:
        """The next transcript update, or None if nothing new was heard within `timeout` seconds."""
        try:
            event = await asyncio.wait_for(self._read_event(), timeout)
        except TimeoutError:
            return None
        return Transcript(event["text"], event["final"])

    async def finalize(self) -> None:
        """Asks the helper to settle its in-progress guess now instead of waiting for a longer pause."""
        await self._send("finalize")

    async def speak(self, text: str) -> None:
        """Queues `text` to be read out (after anything already queued)."""
        text = " ".join(text.split())
        if not text:
            return
        self._next_id += 1
        self._utterances[self._next_id] = 0
        self._texts[self._next_id] = text
        self.echo.begin()
        await self._send(f"speak {self._next_id} {text}")

    async def stop_speaking(self) -> None:
        """Cuts off the readout, including anything still queued."""
        await self._send("stop_speaking")

    async def slow_down(self) -> None:
        """Speaks more slowly from now on (a step at a time, down to MIN_RATE). A readout in progress
        starts again from the beginning of the sentence it's in, at the new rate."""
        self.rate = max(MIN_RATE, round(self.rate * SLOW_DOWN_STEP, 3))
        await self._send(f"rate {self.rate}")
        if not (queued := sorted(self._texts)):
            return
        current = self._texts[queued[0]]
        reached = current[: self._reached.get(queued[0], 0)]
        start = max((m.end() for m in re.finditer(r"[.!?]\s+", reached)), default=0)
        rest = [current[start:]] + [self._texts.pop(i) for i in queued[1:]]
        del self._texts[queued[0]]  # cut off below; not to be read out again by another slow_down
        await self.stop_speaking()
        for text in rest:
            await self.speak(text)

    async def set_voice(self, identifier: str) -> None:
        """Reads out with voice `identifier` from the next utterance on. `voice` follows (at once if it is
        one of `voices`); voicesvc reports a voice that isn't installed on stderr and keeps the current one."""
        await self._send(f"voice {identifier}")
        self.voice = next((v for v in self.voices if v.identifier == identifier), self.voice)

    async def set_mic(self, on: bool) -> None:
        """Opens or closes the microphone. `mic_on` follows once the helper confirms."""
        await self._send(f"mic {'on' if on else 'off'}")

    async def _send(self, command: str) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(command.encode() + b"\n")
        await self._proc.stdin.drain()

    async def _read_event(self) -> dict:
        assert self._proc and self._proc.stdout
        while line := await self._proc.stdout.readline():
            event = json.loads(line)
            if event["event"] == "error":
                raise VoiceError(event["error"], event["message"])
            if event["event"] == "speaking" and event["id"] in self._utterances:
                words = spoken_words(event["spoken"])
                self.echo.spoke(words[self._utterances[event["id"]] :])
                self._utterances[event["id"]] = len(words)
                self._reached[event["id"]] = len(event["spoken"])
            elif event["event"] == "spoken" and self._utterances.pop(event["id"], None) is not None:
                self._texts.pop(event["id"], None)
                self._reached.pop(event["id"], None)
                self.echo.done()
            elif event["event"] == "voice":
                if "error" in event:
                    print(f"voice: {event['error']}", file=sys.stderr, flush=True)
                else:
                    self.voice = Voice.of(event["voice"])
            elif event["event"] == "mic":
                self.mic_on = event["on"]
                if "error" in event:
                    print(f"voice: microphone unavailable: {event['error']}", file=sys.stderr, flush=True)
            if event["event"] in ("ready", "status", "text"):
                return event
        raise VoiceError("helper_exited", f"voicesvc exited (code {await self._proc.wait()})")
