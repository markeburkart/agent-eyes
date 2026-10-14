"""Agent Eyes command line.

    agent-eyes                       open the app in the browser (camera views for now)
    agent-eyes --allow-builtin       ... also showing the Mac's own cameras
    agent-eyes camera list
    agent-eyes camera capture --view desk --detail inspect --region 0.4,0.4,0.2,0.2 --out cap.jpg

Every command builds and spawns the camsvc helper unless --url points at one already running.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .camera import CameraError, ContinuityCameraSource, Region
from .camera.continuity import build_helper


def main() -> None:
    helper = argparse.ArgumentParser(add_help=False)
    helper.add_argument("--url", help="attach to a running camsvc instead of spawning one")
    helper.add_argument("--allow-builtin", action="store_true", help="also expose the Mac's own cameras")

    p = argparse.ArgumentParser(prog="agent-eyes", description="Hands-free voice and vision assistant for the workbench.")
    sub = p.add_subparsers(dest="cmd", metavar="{ui,camera}")
    ui = sub.add_parser("ui", parents=[helper], help="open the app in the browser (default)")
    ui.add_argument("--port", type=int, default=8765, help="port for the web UI (default 8765)")
    ui.add_argument("--open", default="front,desk", help="cameras to open at startup (default front,desk)")
    ui.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")

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
        from .web import serve

        source = make_source(args, tuple(filter(None, args.open.split(","))))
        serve(source, port=args.port, open_browser=not args.no_browser)
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
