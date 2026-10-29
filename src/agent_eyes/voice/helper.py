"""Transcript source and speech output backed by the Swift `voicesvc` helper (on-device SpeechAnalyzer)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .echo import Echo, spoken_words

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BINARY = REPO_ROOT / "voice-service" / ".build" / "release" / "voicesvc"


class VoiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


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
        startup_timeout: float = 120.0,
    ):
        """`context` biases recognition (pass the wake words); `audio_file` replaces the mic, for testing.
        `mic=False` starts with the microphone closed (speaking only). The startup timeout covers a
        one-time speech model download."""
        self.echo = Echo()
        self.mic_on = False
        self._utterances: dict[int, int] = {}  # id -> words of it already added to echo
        self._next_id = 0
        self._binary = Path(binary or os.environ.get("AGENT_EYES_VOICESVC", DEFAULT_BINARY))
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
        self.echo.begin()
        await self._send(f"speak {self._next_id} {text}")

    async def stop_speaking(self) -> None:
        """Cuts off the readout, including anything still queued."""
        await self._send("stop_speaking")

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
            elif event["event"] == "spoken" and self._utterances.pop(event["id"], None) is not None:
                self.echo.done()
            elif event["event"] == "mic":
                self.mic_on = event["on"]
                if "error" in event:
                    print(f"voice: microphone unavailable: {event['error']}", file=sys.stderr, flush=True)
            if event["event"] in ("ready", "status", "text"):
                return event
        raise VoiceError("helper_exited", f"voicesvc exited (code {await self._proc.wait()})")
