"""The agent session behind the dashboard: runs EyesAgent turns and records what happens.

Everything the page shows (what was said, typed or spoken, each capture with its reason and
region, turn timing and cost, status and voice changes) becomes one numbered event. The page polls `events_after(seq)`.
Events also go to transcript.jsonl and capture images to the session folder, for review after
a pilot session.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..agent import CaptureBudget, CaptureEvent, EyesAgent, TurnResult
from ..camera import CameraSource, Capture, Detail, Region
from .voice import LiveVoice


class FrontAlias:
    """Serves the agent's "front" view from another camera, e.g. the Mac's own for a dry run."""

    def __init__(self, source: CameraSource, ref: str):
        self._source, self._ref = source, ref

    async def start(self) -> None:
        await self._source.start()

    async def close(self) -> None:
        await self._source.close()

    async def list_cameras(self):
        return await self._source.list_cameras()

    async def capture(self, view: str, detail: Detail = "overview", region: Region | None = None, reason: str = "") -> Capture:
        return await self._source.capture(self._ref if view == "front" else view, detail, region, reason)


class LiveSession:
    """Lives on the backend's event loop. Thread-safe readers: events_after()."""

    def __init__(self, camera: CameraSource, budget: CaptureBudget, out_dir: Path, *, model: str):
        self.camera = camera
        self.budget = budget
        self.model = model
        self.out_dir = out_dir
        self.state = "starting"
        """starting | ready | thinking | error"""
        self.error: str | None = None
        self.cost_usd: float | None = None
        self.voice: LiveVoice | None = None
        self._agent: EyesAgent | None = None
        self._turn: asyncio.Task | None = None
        self._turn_no = 0
        self._captures: dict[str, tuple[str, bytes]] = {}
        self._events: list[dict] = []
        self._changed = threading.Condition()
        out_dir.mkdir(parents=True, exist_ok=True)
        self._log = (out_dir / "transcript.jsonl").open("a", buffering=1)

    # ---- lifecycle (on the loop) ---------------------------------------------

    async def start(self) -> None:
        self._set_state("starting")
        agent = EyesAgent(self.camera, self.budget, model=self.model, on_capture=self._on_capture)
        try:
            await agent.__aenter__()
        except Exception as e:
            self._fail(f"agent did not start: {e}")
            return
        self._agent = agent
        self.push("status", text=f"agent ready ({self.model})")
        self._set_state("ready")

    async def close(self) -> None:
        if self.voice:
            await self.voice.close()
        if self._turn:
            self._turn.cancel()
        if self._agent:
            agent, self._agent = self._agent, None
            try:
                await agent.__aexit__(None, None, None)
            except Exception:
                pass
        self._log.close()

    async def restart(self) -> None:
        """Fresh conversation: the agent forgets everything said so far."""
        if self.state == "starting":
            return
        if self._turn:
            self._turn.cancel()
        if self._agent:
            agent, self._agent = self._agent, None
            try:
                await agent.__aexit__(None, None, None)
            except Exception:
                pass
        self.cost_usd = None
        self.push("status", text="new conversation")
        await self.start()

    # ---- turns ---------------------------------------------------------------

    async def ask(self, text: str, *, voice: bool = False) -> None:
        """Starts a turn in the background (`voice`: the request was spoken). Raises RuntimeError if the
        agent can't take one now."""
        if self.state != "ready" or self._agent is None:
            raise RuntimeError("the agent is busy" if self.state == "thinking" else f"the agent is {self.state}")
        self._turn_no += 1
        self.push("user", text=text, turn=self._turn_no, **({"voice": True} if voice else {}))
        self._set_state("thinking")
        self._turn = asyncio.create_task(self._run_turn(self._agent, text, self._turn_no))

    async def interrupt(self) -> None:
        if self.state == "thinking" and self._agent:
            self.push("status", text="stop requested")
            await self._agent.interrupt()
        if self.voice:
            await self.voice.hush()

    async def _run_turn(self, agent: EyesAgent, text: str, turn: int) -> None:
        started = time.monotonic()
        if self.voice:
            self.voice.turn_started()
        try:
            async for part in agent.ask(text):
                if isinstance(part, TurnResult):
                    self.cost_usd = part.cost_usd
                    self.push("turn", turn=turn, duration_ms=part.duration_ms, cost_usd=part.cost_usd, is_error=part.is_error)
                else:
                    self.push("agent", text=part, turn=turn)
                    if self.voice:
                        await self.voice.say(part)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fail(f"turn failed after {time.monotonic() - started:.1f}s: {e}")
            return
        finally:
            self._turn = None
            if self.voice:
                self.voice.turn_ended()
        self._set_state("ready")

    def _on_capture(self, e: CaptureEvent) -> None:
        if e.capture is None:
            self.push("capture", turn=self._turn_no, reason=e.reason, view=e.view, detail=e.detail, error=e.error, message=e.message)
            return
        cap = e.capture
        cid = f"{len(self._captures) + 1:03d}-{e.detail}"
        self._captures[cid] = (cap.mime, cap.image)
        (self.out_dir / f"{cid}.jpg").write_bytes(cap.image)
        self.push(
            "capture",
            turn=self._turn_no,
            id=cid,
            image=f"/api/session/captures/{cid}",
            reason=e.reason,
            view=e.view,
            detail=e.detail,
            camera=cap.camera.name,
            region=cap.region and asdict(cap.region),
            width=cap.width,
            height=cap.height,
            source_size=list(cap.source_size),
            sharpness=cap.sharpness,
            frame_age_ms=cap.extra.get("frame_age_ms"),
        )

    # ---- readers -------------------------------------------------------------

    def image(self, cid: str) -> tuple[str, bytes] | None:
        return self._captures.get(cid)

    def events_after(self, seq: int, wait: float = 0) -> list[dict]:
        """Events with a higher seq; blocks up to `wait` seconds for one to arrive."""
        with self._changed:
            if wait and (not self._events or self._events[-1]["seq"] <= seq):
                self._changed.wait(wait)
            return [e for e in self._events[-500:] if e["seq"] > seq]

    def summary(self) -> dict:
        return {"state": self.state, "error": self.error, "cost_usd": self.cost_usd, "model": self.model,
                "out_dir": str(self.out_dir), "budget": self.budget.state(),
                "voice": self.voice.summary() if self.voice else None}

    # ---- internals -----------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state != "error":
            self.error = None
        if state != self.state:
            self.state = state
            self.push("state", state=state)

    def _fail(self, message: str) -> None:
        self.error = message
        self.push("status", text=message, error=True)
        self._set_state("error")

    def push(self, type: str, **fields) -> None:
        with self._changed:
            event = {"seq": len(self._events) + 1, "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                     "type": type, **fields}
            self._events.append(event)
            self._changed.notify_all()
        self._log.write(json.dumps(event) + "\n")
