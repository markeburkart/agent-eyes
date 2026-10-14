"""Manual camera check.

    uv run python -m agent_eyes.camera list
    uv run python -m agent_eyes.camera capture --view desk --detail inspect --region 0.4,0.4,0.2,0.2 --out cap.jpg
"""

import argparse
import asyncio
import sys

from . import CameraError, Region, make_camera_source


async def main() -> int:
    p = argparse.ArgumentParser(prog="agent_eyes.camera")
    p.add_argument("--url", help="attach to a running camsvc instead of spawning one")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("capture")
    c.add_argument("--view", default="front")
    c.add_argument("--detail", choices=["overview", "inspect"], default="overview")
    c.add_argument("--region", help="x,y,w,h normalized 0..1")
    c.add_argument("--out", default="capture.jpg")
    args = p.parse_args()

    opens = () if args.cmd == "list" else (args.view,)
    source = make_camera_source(base_url=args.url, open_at_start=opens)
    try:
        async with source:
            if args.cmd == "list":
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


sys.exit(asyncio.run(main()))
