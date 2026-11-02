"""Agent Eyes command line.

    agent-eyes                       start the camera, the agent and its dashboard in the browser
    agent-eyes --per-turn 1 --per-minute 4    ... with a tighter capture budget
    agent-eyes --allow-builtin --front-camera "MacBook Pro Camera"    dry run with the Mac's camera
    agent-eyes --voice               ... hands-free; the page shows whether a voice conversation is live
    agent-eyes camera list
    agent-eyes camera capture --view desk --detail inspect --region 0.4,0.4,0.2,0.2 --out cap.jpg
    agent-eyes chat                  talk to the agent by typing (one line per turn)
    agent-eyes chat --voice          ... or hands-free: say "hey eyes", then the request; replies are read out
                                     (in the chat, /voice and /speech switch each on and off)

Every command builds and spawns the camsvc helper unless --url points at one already running.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import threading

from .camera import CameraError, CameraSource, ContinuityCameraSource, Region
from .camera.continuity import REPO_ROOT, build_helper
from .voice import (
    DEFAULT_WAKE_WORDS,
    Command,
    ConversationEnded,
    ConversationOpened,
    Focus,
    FollowUpHeard,
    Interrupted,
    SlowedDown,
    VoiceError,
    VoiceHelper,
    WakeHeard,
    chime,
    voice_commands,
)
from .voice import build_helper as build_voice_helper


def main() -> None:
    helper = argparse.ArgumentParser(add_help=False)
    helper.add_argument("--url", help="attach to a running camsvc instead of spawning one")
    helper.add_argument("--allow-builtin", action="store_true", help="also expose the Mac's own cameras")

    p = argparse.ArgumentParser(prog="agent-eyes", description="Hands-free voice and vision assistant for the workbench.")
    sub = p.add_subparsers(dest="cmd", metavar="{ui,camera,chat}")
    budget = argparse.ArgumentParser(add_help=False)
    budget.add_argument("--model", default=None, help="Claude model (default: the agent's default)")
    budget.add_argument("--per-turn", type=int, default=3, help="max captures per turn (default 3)")
    budget.add_argument("--per-minute", type=int, default=10, help="max captures per rolling minute (default 10)")

    voice = argparse.ArgumentParser(add_help=False)
    voice.add_argument("--voice", action="store_true",
                       help="start with voice commands on: wake word, then dictation (toggle with /voice or on the page)")
    voice.add_argument("--wake-word", default=None,
                       help=f"comma-separated wake phrases (default {','.join(DEFAULT_WAKE_WORDS)})")
    voice.add_argument("--pause", type=float, default=1.5, help="seconds of silence that end a spoken request (default 1.5)")
    voice.add_argument("--follow-up", type=float, default=30.0,
                       help="seconds a voice conversation stays open for follow-ups without the wake word (default 30, 0 = off)")
    voice.add_argument("--speech", action=argparse.BooleanOptionalAction, default=None,
                       help="read replies out (toggle with /speech or on the page; default: on with --voice)")
    voice.add_argument("--locale", default=None, help="speech locale, e.g. en-US (default: the system's)")
    voice.add_argument("--voice-file", default=None, help=argparse.SUPPRESS)  # play an audio file instead of the mic (testing)

    ui = sub.add_parser("ui", parents=[helper, budget, voice], help="start the agent and its dashboard in the browser (default)")
    ui.add_argument("--port", type=int, default=8765, help="port for the web UI (default 8765)")
    ui.add_argument("--front-camera", default="front", metavar="REF",
                    help='camera the agent sees as "front": a role, id or name (default: the phone, role front)')
    ui.add_argument("--open", help="cameras to open at startup (default: the front camera)")
    ui.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")

    sub.add_parser("chat", parents=[helper, budget, voice], help="talk to the agent by typing")

    cam = sub.add_parser("camera", help="manual camera checks").add_subparsers(dest="camera_cmd", required=True)
    cam.add_parser("list", parents=[helper], help="list cameras")
    c = cam.add_parser("capture", parents=[helper], help="capture one image to a file")
    c.add_argument("--view", default="front")
    c.add_argument("--detail", choices=["overview", "inspect"], default="overview")
    c.add_argument("--region", help="x,y,w,h normalized 0..1")
    c.add_argument("--out", default="capture.jpg")

    # No subcommand (or only options) means the app.
    argv = sys.argv[1:]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["ui", *argv]
    args = p.parse_args(argv)

    if args.cmd == "ui":
        from datetime import datetime

        from .agent import DEFAULT_MODEL, CaptureBudget
        from .web import VoiceOptions, serve

        source = make_source(args, tuple(filter(None, (args.open or args.front_camera).split(","))))
        serve(
            source,
            front=args.front_camera,
            budget=CaptureBudget(per_turn=args.per_turn, per_minute=args.per_minute),
            out_dir=REPO_ROOT / "captures" / "sessions" / f"{datetime.now():%Y%m%d-%H%M%S}",
            model=args.model or DEFAULT_MODEL,
            voice=VoiceOptions(
                wake_words=wake_words_of(args),
                pause=args.pause,
                follow_up=args.follow_up,
                locale=args.locale,
                audio_file=args.voice_file,
                listening=args.voice,
                speaking=args.voice if args.speech is None else args.speech,
            ),
            port=args.port,
            open_browser=not args.no_browser,
        )
    elif args.cmd == "chat":
        # Treat kill and a closed terminal like Ctrl-C so the agent and helpers shut down cleanly.
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.default_int_handler)
        try:
            sys.exit(asyncio.run(chat_command(args)))
        except KeyboardInterrupt:
            sys.exit(130)  # Ctrl-C is how a voice chat ends
    else:
        sys.exit(asyncio.run(camera_command(args)))


def make_source(args: argparse.Namespace, open_at_start: tuple[str, ...]) -> ContinuityCameraSource:
    # Keep the spawned helper in step with its sources (skipped for a custom binary or a running helper).
    if args.url is None and "AGENT_EYES_CAMSVC" not in os.environ:
        print("building camsvc ...", file=sys.stderr, flush=True)
        try:
            build_helper()
        except CameraError as e:
            sys.exit(f"error [{e.code}]: {e.message}")
    return ContinuityCameraSource(base_url=args.url, open_at_start=open_at_start, allow_builtin=args.allow_builtin)


async def camera_command(args: argparse.Namespace) -> int:
    opens = () if args.camera_cmd == "list" else (args.view,)
    source = make_source(args, opens)
    try:
        async with source:
            if args.camera_cmd == "list":
                for cam in await source.list_cameras():
                    print(cam)
                return 0
            region = Region(*map(float, args.region.split(","))) if args.region else None
            cap = await source.capture(args.view, args.detail, region, reason="manual check")
            with open(args.out, "wb") as f:
                f.write(cap.image)
            print(f"{args.out}: {cap.width}x{cap.height} from {cap.camera.name} {cap.source_size} "
                  f"region={cap.region} sharpness={cap.sharpness:.1f} extra={cap.extra}")
            return 0
    except CameraError as e:
        print(f"error [{e.code}]: {e.message}", file=sys.stderr)
        return 1


async def chat_command(args: argparse.Namespace, source: CameraSource | None = None) -> int:
    """Conversation with the agent, typed or spoken, with replies optionally read out. Captures are saved
    under captures/chat/ for review."""
    from .agent import DEFAULT_MODEL, CaptureBudget, CaptureEvent, EyesAgent, TurnResult

    out_dir = REPO_ROOT / "captures" / "chat"
    out_dir.mkdir(parents=True, exist_ok=True)

    def show_capture(e: CaptureEvent) -> None:
        if e.capture is None:
            print(f"  [look {e.view} {e.detail} failed: {e.error}] {e.reason}", flush=True)
            return
        cap = e.capture
        path = out_dir / f"{cap.captured_at:%Y%m%d-%H%M%S-%f}-{e.view}-{e.detail}.jpg"
        path.write_bytes(cap.image)
        region = f" region={cap.region.x:.2f},{cap.region.y:.2f},{cap.region.w:.2f},{cap.region.h:.2f}" if cap.region else ""
        print(f"  [look {e.view} {e.detail}{region} {cap.width}x{cap.height}] {e.reason} -> {path.name}", flush=True)

    # Requests from the keyboard and the microphone, in the order they arrive. None ends the chat.
    requests: asyncio.Queue[str | None | Exception] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    interactive = sys.stdin.isatty()

    def read_stdin() -> None:
        # A daemon thread rather than asyncio.to_thread, so Ctrl-C doesn't wait for a pending readline.
        while line := sys.stdin.readline():
            if (text := line.strip()).startswith("/"):
                # Toggles apply right away, even while the agent is answering or a reply is being read out.
                asyncio.run_coroutine_threadsafe(voice.command(text), loop)
            elif text:
                loop.call_soon_threadsafe(requests.put_nowait, text)
        if not voice.listening:
            loop.call_soon_threadsafe(requests.put_nowait, None)

    source = source or make_source(args, ("front",))
    try:
        async with contextlib.AsyncExitStack() as stack:
            voice = VoiceControls(args, requests, stack)
            await voice.set(listening=args.voice, speaking=args.voice if args.speech is None else args.speech)
            if voice.listening or voice.speaking:
                print(f"  [{voice.status()}; /help for the toggles]", flush=True)
            await source.start()
            stack.push_async_callback(source.close)
            agent = await stack.enter_async_context(EyesAgent(
                source,
                CaptureBudget(per_turn=args.per_turn, per_minute=args.per_minute),
                model=args.model or DEFAULT_MODEL,
                on_capture=show_capture,
            ))
            # Stop listening before the agent and camera close (the exit stack unwinds in reverse).
            stack.push_async_callback(voice.close)
            threading.Thread(target=read_stdin, daemon=True).start()
            while True:
                if interactive and not voice.listening:
                    print("you> ", end="", flush=True)
                text = await requests.get()
                if text is None:
                    return 0
                if isinstance(text, Exception):
                    raise text
                if not interactive and not voice.listening:
                    print(f"you> {text}", flush=True)
                voice.focus.busy = True
                try:
                    async for part in agent.ask(text):
                        if isinstance(part, TurnResult):
                            # The SDK reports the session's running total, not this turn's cost.
                            cost = f", session ${part.cost_usd:.4f}" if part.cost_usd is not None else ""
                            print(f"  ({part.duration_ms / 1000:.1f}s{cost}{' error' if part.is_error else ''})", flush=True)
                        else:
                            print(f"eyes> {part}", flush=True)
                            await voice.say(part)
                finally:
                    voice.focus.busy = False
                    voice.focus.extend()
    except CameraError as e:
        print(f"error [{e.code}]: {e.message}", file=sys.stderr)
        return 1
    except VoiceError as e:
        print(f"voice error [{e.code}]: {e.message}", file=sys.stderr)
        return 1


def set_title(title: str) -> None:
    """Terminal tab/window title: a status you can glance at from across the bench."""
    if sys.stdout.isatty():
        print(f"\033]0;{title}\007", end="", flush=True)


def wake_words_of(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(w.strip() for w in args.wake_word.split(",") if w.strip()) if args.wake_word else DEFAULT_WAKE_WORDS


def start_voice(args: argparse.Namespace, mic: bool) -> VoiceHelper:
    if "AGENT_EYES_VOICESVC" not in os.environ:
        print("building voicesvc ...", file=sys.stderr, flush=True)
        build_voice_helper()
    # A test audio file stands in for the mic; keep the readout silent then.
    return VoiceHelper(locale=args.locale, context=wake_words_of(args), audio_file=args.voice_file,
                       speech_volume=0.0 if args.voice_file else 1.0, mic=mic)


class VoiceControls:
    """The chat's two voice toggles: voice commands (the mic is open, listening for the wake word)
    and speech (replies are read out). voicesvc starts the first time either is switched on."""

    HELP = "/voice [on|off]  voice commands    /speech [on|off]  read replies out    /help"

    def __init__(self, args: argparse.Namespace, requests: asyncio.Queue[str | None | Exception],
                 stack: contextlib.AsyncExitStack):
        self.listening = False
        self.speaking = False
        # After a spoken request, follow-ups don't need the wake word until things go quiet.
        self.focus = Focus(args.follow_up)
        self.wake_words = wake_words_of(args)
        self.pause = args.pause
        self._args = args
        self._requests = requests
        self._stack = stack
        self._helper: VoiceHelper | None = None
        self._listen_task: asyncio.Task | None = None

    async def set(self, *, listening: bool | None = None, speaking: bool | None = None) -> None:
        listening = self.listening if listening is None else listening
        speaking = self.speaking if speaking is None else speaking
        if self._helper is None and (listening or speaking):
            self._helper = await self._stack.enter_async_context(start_voice(self._args, mic=listening))
            self._listen_task = asyncio.create_task(listen(self, self._helper, self._requests))
        elif self._helper and listening != self.listening:
            await self._helper.set_mic(listening)
        if self._helper and self.speaking and not speaking:
            await self._helper.stop_speaking()
        if self.listening and not listening:
            self.focus.end()
        self.listening, self.speaking = listening, speaking
        set_title(self.idle_title if listening else "")

    @property
    def idle_title(self) -> str:
        return f'agent-eyes: say "{self.wake_words[0]}"'

    def status(self) -> str:
        if self.listening:
            listening = f'voice commands on (say "{self.wake_words[0]}", then your request)'
        else:
            listening = "voice commands off"
        return f"{listening}, speech {'on' if self.speaking else 'off'}"

    async def command(self, text: str) -> None:
        """Handles a typed /command."""
        name, _, value = text[1:].partition(" ")
        value = value.strip().lower()
        if name in ("voice", "speech") and value in ("", "on", "off"):
            current = self.listening if name == "voice" else self.speaking
            on = not current if not value else value == "on"
            try:
                await self.set(**{"listening" if name == "voice" else "speaking": on})
            except VoiceError as e:
                print(f"voice error [{e.code}]: {e.message}", file=sys.stderr, flush=True)
            print(f"  [{self.status()}]", flush=True)
        else:
            print(f"  [{self.HELP}]", flush=True)

    async def say(self, text: str) -> None:
        if self.speaking and self._helper:
            await self._helper.speak(text)

    async def close(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
        set_title("")


async def listen(controls: VoiceControls, voice: VoiceHelper, requests: asyncio.Queue[str | None | Exception]) -> None:
    """Puts each spoken request (wake word, or any speech while the conversation is live, until a pause) on `requests`."""
    wake_words = controls.wake_words
    focus = controls.focus
    try:
        async for event in voice_commands(voice, wake_words, focus=focus, pause=controls.pause):
            if isinstance(event, WakeHeard):
                chime("Tink")
                print("  [listening ...]", flush=True)
            elif isinstance(event, FollowUpHeard):
                continue
            elif isinstance(event, Command):
                if not controls.listening:
                    continue  # cut off by /voice off
                chime("Pop")
                print(f"you (voice)> {event.text}", flush=True)
                requests.put_nowait(event.text)
            elif isinstance(event, ConversationOpened):
                set_title("● agent-eyes: conversation live")
                print(f"  [conversation live: follow up without the wake word; closes after {focus.seconds:g}s of quiet "
                      "or when you say \"ok thanks\" or \"that's all\"]", flush=True)
            elif isinstance(event, Interrupted):
                print(f'  [readout stopped: you said "{event.word}"]', flush=True)
            elif isinstance(event, SlowedDown):
                print("  [speaking more slowly]", flush=True)
            elif isinstance(event, ConversationEnded):
                if controls.listening:  # otherwise /voice off closed it
                    chime("Submarine")
                    set_title(controls.idle_title)
                    print(f'  [conversation closed: {event.reason}; say "{wake_words[0]}" to start again]', flush=True)
            else:
                chime("Bottle")
                print(f"  [{event.reason}{': ' + event.text if event.text else ''}]", flush=True)
    except VoiceError as e:
        requests.put_nowait(e)
