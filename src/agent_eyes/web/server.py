"""Local web server for the browser views.

  /         the agent dashboard: transcript, live view of the agent's camera, filmstrip of the
            agent's captures (reason and region) and the capture budget with its controls
  /cameras  the camera page: live previews, per-camera status, manual captures and helper events

The agent runs in a LiveSession on the backend's event loop, with voice commands and replies read
out (LiveVoice) in this process too, so the page can show the voice state. Manual captures go through the
CameraSource (same detail levels as the agent); previews, status and events are proxied straight
from the camsvc helper. Previews never open or reopen a stream, so they don't interfere with the
helper's own stall recovery.
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
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from ..agent import CaptureBudget
from ..camera import CameraError, ContinuityCameraSource, Region
from .live import FrontAlias, LiveSession
from .voice import LiveVoice, VoiceOptions

STATIC = Path(__file__).with_name("static")
PAGES = {"/": "agent.html", "/cameras": "cameras.html"}


class Backend:
    """Runs the async camera source and the agent on their own event loop so threaded request handlers can use them."""

    def __init__(self, source: ContinuityCameraSource, front: str, budget: CaptureBudget, out_dir: Path, model: str,
                 voice: VoiceOptions):
        self.source = source
        self.front = front
        """Camera reference the agent's "front" view uses (normally the role "front" itself)."""
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(source.start())
        self.http = httpx.Client(base_url=source.base_url or "", timeout=10.0)
        camera = source if front == "front" else FrontAlias(source, front)
        self.session = LiveSession(camera, budget, out_dir, model=model)
        self.session.voice = LiveVoice(self.session, voice)
        # The agent (and voicesvc) take a few seconds to start; serve the page meanwhile.
        self.spawn(self.session.start())
        self.spawn(self.session.voice.set(listening=voice.listening, speaking=voice.speaking))

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def spawn(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn, *args):
        """Runs a plain function on the loop (the session and budget aren't thread-safe)."""
        async def wrapper():
            return fn(*args)
        return self.run(wrapper())

    def close(self) -> None:
        self.http.close()
        try:
            self.run(asyncio.wait_for(self.session.close(), 10))
        except Exception:
            pass
        finally:
            self.run(self.source.close())
            self.loop.call_soon_threadsafe(self.loop.stop)

    def session_summary(self) -> dict:
        return {**self.call(self.session.summary), "camera": self.front}

    def set_budget(self, body: dict) -> None:
        budget = self.session.budget
        def apply():
            for k in ("per_turn", "per_minute"):
                if k in body:
                    v = int(body[k])
                    if not 0 <= v <= 100:
                        raise ValueError(f"{k} must be 0..100")
                    setattr(budget, k, v)
            if "paused" in body:
                budget.paused = bool(body["paused"])
        self.call(apply)

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
            self._proxy("GET", url.path.removeprefix("/api") + ("?" + urlencode(query, quote_via=quote) if query else ""))
        elif url.path == "/api/session":
            self._json(200, self.backend.session_summary())
        elif url.path == "/api/session/events":
            try:
                after, wait = int(query.get("after", 0)), min(float(query.get("wait", 0)), 20)
            except ValueError:
                return self._json(400, {"error": "bad_request", "message": "after and wait must be numbers"})
            events = self.backend.session.events_after(after, wait)
            self._json(200, {"events": events, "session": self.backend.session_summary()})
        elif url.path.startswith("/api/session/captures/"):
            img = self.backend.session.image(url.path.rsplit("/", 1)[1])
            if img:
                self._send(200, img[0], img[1])
            else:
                self._json(404, {"error": "not_found", "message": url.path})
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
        elif self.path == "/api/session/ask":
            text = str(body.get("text", "")).strip()
            if not text:
                return self._json(400, {"error": "bad_request", "message": "text is empty"})
            try:
                self.backend.run(self.backend.session.ask(text))
            except RuntimeError as e:
                return self._json(409, {"error": "busy", "message": str(e)})
            self._json(200, self.backend.session_summary())
        elif self.path == "/api/session/stop":
            self.backend.spawn(self.backend.session.interrupt())
            self._json(200, self.backend.session_summary())
        elif self.path == "/api/session/restart":
            self.backend.spawn(self.backend.session.restart())
            self._json(200, self.backend.session_summary())
        elif self.path == "/api/session/voice":
            # voicesvc can take a while to start (or a mic permission prompt can be waiting): don't wait for it.
            change = {k: bool(body[k]) for k in ("listening", "speaking") if k in body}
            self.backend.spawn(self.backend.session.voice.set(**change))
            self._json(200, self.backend.session_summary())
        elif self.path == "/api/session/voice/end":
            self.backend.call(self.backend.session.voice.end_conversation)
            self._json(200, self.backend.session_summary())
        elif self.path == "/api/session/budget":
            try:
                self.backend.set_budget(body)
            except (TypeError, ValueError) as e:
                return self._json(400, {"error": "bad_request", "message": str(e)})
            self._json(200, self.backend.session_summary())
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


def serve(
    source: ContinuityCameraSource,
    *,
    front: str,
    budget: CaptureBudget,
    out_dir: Path,
    model: str,
    voice: VoiceOptions = VoiceOptions(),
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Starts the camera source and the agent, and serves the views until Ctrl-C, kill or the terminal closes."""
    try:
        Handler.backend = Backend(source, front, budget, out_dir, model, voice)
    except CameraError as e:
        raise SystemExit(f"error [{e.code}]: {e.message}")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # Port taken (often another agent-eyes still running): use any free port rather than fail.
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    page = f"http://127.0.0.1:{server.server_port}/"
    print(f"Agent Eyes on {page} (camera helper {source.base_url}); Ctrl-C to stop", flush=True)
    print(f"session log and captures: {out_dir}", flush=True)
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
        # Closing the agent takes a moment; a second Ctrl-C must not leave the helper running.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        server.server_close()
        Handler.backend.close()
