"""CameraSource backed by the Swift `camsvc` helper (Continuity Camera, Desk View, USB cameras).

All captures are video frames: the helper keeps streams open and returns the sharpest
recent frame. `overview` downsamples the frame; `inspect` crops at native resolution.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx

from .base import CameraError, CameraInfo, Capture, Detail, Region

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BINARY = REPO_ROOT / "camera-service" / ".build" / "release" / "camsvc"


def build_helper() -> None:
    """Builds (or incrementally rebuilds) the default camsvc binary; a no-op build takes well under a second."""
    cmd = ["swift", "build", "-c", "release", "--package-path", str(REPO_ROOT / "camera-service")]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise CameraError("helper_build_failed", "swift not found; install the Xcode command line tools")
    if result.returncode != 0:
        raise CameraError("helper_build_failed", f"{' '.join(cmd)} failed:\n{result.stdout}{result.stderr}")

# Long-edge pixel caps per detail level. Current Claude models accept up to 2576px; the
# overview cap keeps a whole-scene look around ~1.2k image tokens.
MAX_EDGE: dict[Detail, int] = {"overview": 1280, "inspect": 2576}
JPEG_QUALITY: dict[Detail, float] = {"overview": 0.8, "inspect": 0.9}


class ContinuityCameraSource:
    def __init__(
        self,
        *,
        binary: Path | str | None = None,
        base_url: str | None = None,
        open_at_start: tuple[str, ...] = ("front", "desk"),
        allow_builtin: bool = False,
        startup_timeout: float = 20.0,
    ):
        """Spawns the helper unless `base_url` points at one that is already running."""
        self._binary = Path(binary or os.environ.get("AGENT_EYES_CAMSVC", DEFAULT_BINARY))
        self._base_url = base_url
        self._open_at_start = open_at_start
        self._allow_builtin = allow_builtin
        self._startup_timeout = startup_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._base_url is None:
            await self._spawn()
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=15.0)
        await self._get("/health")

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except TimeoutError:
                self._proc.kill()
        if self._stdout_task:
            self._stdout_task.cancel()

    @property
    def base_url(self) -> str | None:
        """URL of the helper's HTTP API (known once started), for diagnostics such as the test UI."""
        return self._base_url

    async def __aenter__(self) -> ContinuityCameraSource:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def list_cameras(self) -> list[CameraInfo]:
        data = await self._get("/cameras")
        return [_camera_info(c) for c in data["cameras"]]

    async def capture(
        self,
        view: str,
        detail: Detail = "overview",
        region: Region | None = None,
        reason: str = "",
    ) -> Capture:
        body: dict = {"camera": view, "max_edge": MAX_EDGE[detail], "quality": JPEG_QUALITY[detail]}
        if region is not None:
            body["region"] = {"x": region.x, "y": region.y, "w": region.w, "h": region.h}
        d = await self._post("/capture", body)
        r = d["region"]
        full = r["x"] == 0 and r["y"] == 0 and r["w"] == 1 and r["h"] == 1
        return Capture(
            image=base64.b64decode(d["image"]["base64"]),
            mime=d["image"]["mime"],
            width=d["image"]["width"],
            height=d["image"]["height"],
            camera=CameraInfo(id=d["camera"]["id"], name=d["camera"]["name"], kind=d["camera"]["kind"]),
            detail=detail,
            reason=reason,
            region=None if full else Region(r["x"], r["y"], r["w"], r["h"]),
            source_size=(d["source"]["width"], d["source"]["height"]),
            captured_at=datetime.fromisoformat(d["captured_at"]),
            sharpness=d["sharpness"],
            extra={
                "crop_px": d["crop"],
                "candidate_sharpness": d["candidate_sharpness"],
                "frame_age_ms": d["frame_age_ms"],
            },
        )

    async def _spawn(self) -> None:
        if not self._binary.exists():
            raise CameraError(
                "helper_missing",
                f"camsvc not found at {self._binary}. Build it with: swift build -c release --package-path camera-service",
            )
        args = [str(self._binary), "--port", "0"]
        if self._open_at_start:
            args += ["--open", ",".join(self._open_at_start)]
        if self._allow_builtin:
            args.append("--allow-builtin")
        # Helper logs go straight to our stderr; stdout carries the one-line ready event.
        self._proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=sys.stderr)
        assert self._proc.stdout
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), self._startup_timeout)
        except TimeoutError:
            await self.close()
            raise CameraError("helper_timeout", "camsvc did not become ready (is a camera permission prompt waiting?)")
        if not line:
            raise CameraError("helper_exited", f"camsvc exited during startup (code {await self._proc.wait()})")
        event = json.loads(line)
        if event.get("event") != "ready":
            await self.close()
            raise CameraError(event.get("error", "helper_error"), event.get("message", str(event)))
        self._base_url = f"http://127.0.0.1:{event['port']}"
        self._stdout_task = asyncio.create_task(self._drain_stdout())

    async def _drain_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        while await self._proc.stdout.readline():
            pass

    async def _get(self, path: str) -> dict:
        return await self._request("GET", path)

    async def _post(self, path: str, body: dict) -> dict:
        return await self._request("POST", path, body)

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if self._http is None:
            raise CameraError("not_started", "camera source not started")
        try:
            resp = await self._http.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise CameraError("helper_unreachable", f"camera helper request failed: {e}") from e
        data = resp.json()
        if resp.status_code != 200:
            raise CameraError(data.get("error", "error"), data.get("message", resp.text))
        return data


def _camera_info(c: dict) -> CameraInfo:
    size = (c["frame_width"], c["frame_height"]) if "frame_width" in c else None
    return CameraInfo(
        id=c["id"], name=c["name"], kind=c["kind"], role=c.get("role"),
        connected=c["connected"], streaming=c["streaming"], frame_size=size,
    )
