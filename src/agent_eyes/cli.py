"""Agent Eyes command line.

    agent-eyes                       start the camera, the agent and its dashboard in the browser
    agent-eyes --per-turn 1 --per-minute 4    ... with a tighter capture budget
    agent-eyes --allow-builtin --front-camera "MacBook Pro Camera"    dry run with the Mac's camera
    agent-eyes camera list
    agent-eyes camera capture --view desk --detail inspect --region 0.4,0.4,0.2,0.2 --out cap.jpg
    agent-eyes chat                  talk to the agent by typing (one line per turn)

Every command builds and spawns the camsvc helper unless --url points at one already running.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .camera import CameraError, CameraSource, ContinuityCameraSource, Region
from .camera.continuity import REPO_ROOT, build_helper


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

    ui = sub.add_parser("ui", parents=[helper, budget], help="start the agent and its dashboard in the browser (default)")
    ui.add_argument("--port", type=int, default=8765, help="port for the web UI (default 8765)")
    ui.add_argument("--front-camera", default="front", metavar="REF",
                    help='camera the agent sees as "front": a role, id or name (default: the phone, role front)')
    ui.add_argument("--open", help="cameras to open at startup (default: the front camera)")
    ui.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")

    cam = sub.add_parser("camera", help="manual camera checks").add_subparsers(dest="camera_cmd", required=True)
    cam.add_parser("list", parents=[helper], help="list cameras")
    c = cam.add_parser("capture", parents=[helper], help="capture one image to a file")
    c.add_argument("--view", default="front")
    c.add_argument("--detail", choices=["overview", "inspect"], default="overview")
    c.add_argument("--region", help="x,y,w,h normalized 0..1")
    c.add_argument("--out", default="capture.jpg")

    sub.add_parser("chat", parents=[helper, budget], help="talk to the agent by typing")

    # No subcommand (or only options) means the app.
    argv = sys.argv[1:]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["ui", *argv]
    args = p.parse_args(argv)

    if args.cmd == "ui":
        from datetime import datetime

        from .agent import DEFAULT_MODEL, CaptureBudget
        from .web import serve

        source = make_source(args, tuple(filter(None, (args.open or args.front_camera).split(","))))
        serve(
            source,
            front=args.front_camera,
            budget=CaptureBudget(per_turn=args.per_turn, per_minute=args.per_minute),
            out_dir=REPO_ROOT / "captures" / "sessions" / f"{datetime.now():%Y%m%d-%H%M%S}",
            model=args.model or DEFAULT_MODEL,
            port=args.port,
            open_browser=not args.no_browser,
        )
    elif args.cmd == "chat":
        sys.exit(asyncio.run(chat_command(args)))
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
    """Typed conversation with the agent. Captures are saved under captures/chat/ for review."""
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

    source = source or make_source(args, ("front",))
    interactive = sys.stdin.isatty()
    try:
        async with source, EyesAgent(
            source,
            CaptureBudget(per_turn=args.per_turn, per_minute=args.per_minute),
            model=args.model or DEFAULT_MODEL,
            on_capture=show_capture,
        ) as agent:
            while True:
                if interactive:
                    print("you> ", end="", flush=True)
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    return 0
                if not (text := line.strip()):
                    continue
                if not interactive:
                    print(f"you> {text}", flush=True)
                async for part in agent.ask(text):
                    if isinstance(part, TurnResult):
                        # The SDK reports the session's running total, not this turn's cost.
                        cost = f", session ${part.cost_usd:.4f}" if part.cost_usd is not None else ""
                        print(f"  ({part.duration_ms / 1000:.1f}s{cost}{' error' if part.is_error else ''})", flush=True)
                    else:
                        print(f"eyes> {part}", flush=True)
    except CameraError as e:
        print(f"error [{e.code}]: {e.message}", file=sys.stderr)
        return 1
