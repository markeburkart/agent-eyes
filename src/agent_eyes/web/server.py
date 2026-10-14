"""Local web server for the browser views.

Today it serves the camera page: live previews, per-camera status, captures and helper events.
Captures go through the CameraSource (same detail levels as the agent); previews, status and
events are proxied straight from the camsvc helper. Previews never open or reopen a stream, so
they don't interfere with the helper's own stall recovery.
"""

from __future__ import annotations

import asyncio
import base64
import json
import signal
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..camera import CameraError, ContinuityCameraSource, Region

STATIC = Path(__file__).with_name("static")
PAGES = {"/": "cameras.html", "/cameras": "cameras.html"}


class Backend:
    """Runs the async camera source on its own event loop so threaded request handlers can use it."""

    def __init__(self, source: ContinuityCameraSource):
        self.source = source
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(source.start())
        self.http = httpx.Client(base_url=source.base_url or "", timeout=10.0)

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def close(self) -> None:
        self.http.close()
        self.run(self.source.close())
        self.loop.call_soon_threadsafe(self.loop.stop)

    def capture(self, body: dict) -> dict:
        r = body.get("region")
        region = Region(r["x"], r["y"], r["w"], r["h"]) if r else None
        started = time.monotonic()
        cap = self.run(self.source.capture(body["camera"], body.get("detail", "overview"), region, reason="camera page"))
        return {
            "camera": {"id": cap.camera.id, "name": cap.camera.name, "kind": cap.camera.kind},
            "detail": cap.detail,
            "image": f"data:{cap.mime};base64,{base64.b64encode(cap.image).decode()}",
            "bytes": len(cap.image),
            "width": cap.width,
            "height": cap.height,
            "region": cap.region and asdict(cap.region),
            "source_size": cap.source_size,
            "captured_at": cap.captured_at.isoformat(),
            "sharpness": cap.sharpness,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            **cap.extra,
        }


class Handler(BaseHTTPRequestHandler):
    backend: Backend

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path in PAGES:
            self._send(200, "text/html; charset=utf-8", (STATIC / PAGES[url.path]).read_bytes())
        elif url.path == "/favicon.ico":
            self._send(204, "image/x-icon", b"")
        elif url.path in ("/api/cameras", "/api/events", "/api/preview"):
            self._proxy("GET", url.path.removeprefix("/api") + ("?" + urlencode(query) if query else ""))
        else:
            self._json(404, {"error": "not_found", "message": url.path})

    def do_POST(self) -> None:
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad_request", "message": "body must be JSON"})
        if self.path == "/api/open":
            self._proxy("POST", "/open", body)
        elif self.path == "/api/capture":
            try:
                self._json(200, self.backend.capture(body))
            except CameraError as e:
                self._json(502, {"error": e.code, "message": e.message})
            except (KeyError, TypeError, ValueError) as e:
                self._json(400, {"error": "bad_request", "message": str(e)})
        else:
            self._json(404, {"error": "not_found", "message": self.path})

    def _proxy(self, method: str, path: str, body: dict | None = None) -> None:
        try:
            r = self.backend.http.request(method, path, json=body)
        except httpx.HTTPError as e:
            return self._json(502, {"error": "helper_unreachable", "message": str(e)})
        extra = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}
        self._send(r.status_code, r.headers.get("content-type", "application/json"), r.content, extra)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, "application/json", json.dumps(obj).encode())

    def _send(self, status: int, content_type: str, data: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        # Previews and status are polled several times a second; only log failures
        # (except 409 not_open, which is the normal preview answer for a closed camera).
        if len(args) > 1 and str(args[1]).isdigit() and int(args[1]) >= 400 and int(args[1]) != 409:
            super().log_message(format, *args)


def serve(source: ContinuityCameraSource, port: int = 8765, open_browser: bool = True) -> None:
    """Starts the camera source and serves the views until Ctrl-C, kill or the terminal closes."""
    try:
        Handler.backend = Backend(source)
    except CameraError as e:
        raise SystemExit(f"error [{e.code}]: {e.message}")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # Port taken (often another agent-eyes still running): use any free port rather than fail.
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    page = f"http://127.0.0.1:{server.server_port}/"
    print(f"Agent Eyes on {page} (camera helper {source.base_url}); Ctrl-C to stop", flush=True)
    if open_browser:
        webbrowser.open(page)
    # Treat kill and a closed terminal like Ctrl-C so the spawned helper is always stopped.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.default_int_handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.backend.close()
