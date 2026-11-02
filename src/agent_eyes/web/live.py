"""The agent session behind the dashboard: runs EyesAgent turns and records what happens.

Everything the page shows (what was said, typed or spoken, each capture with its reason and
region, turn timing and cost, status and voice changes) becomes one numbered event. The page polls `events_after(seq)`.
Events also go to transcript.jsonl and capture images to the session folder, for review after
a pilot session.

Popovers: `show_popover(kind, title, payload)` opens a movable dialog on the page ("popover"
event); `dismiss_popovers()` closes them ("popover_close"). Kinds the page doesn't need help with
pass straight through; a kind can register a step in POPOVER_KINDS that checks the payload and adds
what the page needs. "annotated-image" renders the agent's marks over a capture into an SVG, saved
next to the capture. The session keeps the open popovers in z-order (the page reports clicks with
`raise_popover()`), so "dismiss" closes the one on top.

"steps" is a group of annotated images the user steps through, one per step of the agent's
instructions, starting at step 1. `step_popover()` moves it ("popover_step" event; from the page's
buttons or the voice commands "next", "back", "repeat") and reads the step out. While one is open,
the agent's prompt starts with the step on screen. Each step's SVG is saved, with
{id}-steps.json listing the group.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ..agent import CaptureBudget, CaptureEvent, CaptureLog, EyesAgent, TurnResult
from ..agent.annotate import parse_annotations, render_svg
from ..agent.steps import earlier_marks, parse_steps
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


@dataclass(frozen=True)
class StepMove:
    """Where a steps popover is after `LiveSession.step_popover()`."""

    id: str
    step: int
    total: int
    moved: bool


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
        self._capture_log = CaptureLog()  # kept across conversations, so capture numbers stay unique
        self._popover_files: dict[str, tuple[str, bytes]] = {}
        self._popovers: list[str] = []  # open popover ids, topmost last
        self._popover_no = 0
        self._steps: dict[str, dict] = {}  # steps popovers: id -> {"steps": [...], "at": step showing, from 1}
        self._events: list[dict] = []
        self._changed = threading.Condition()
        out_dir.mkdir(parents=True, exist_ok=True)
        self._log = (out_dir / "transcript.jsonl").open("a", buffering=1)

    # ---- lifecycle (on the loop) ---------------------------------------------

    async def start(self) -> None:
        self._set_state("starting")
        agent = EyesAgent(self.camera, self.budget, model=self.model, on_capture=self._on_capture,
                          captures=self._capture_log, show_popover=self.show_popover)
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
        self.dismiss_popovers("all")
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
        # Steps on screen: the agent learns which one, so "is this right?" is about that step.
        prompt = f"{screen}\n{text}" if (screen := self._on_screen()) else text
        self._turn = asyncio.create_task(self._run_turn(self._agent, prompt, self._turn_no))

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
        cid = self._cid(e.number, e.detail)
        self._captures[cid] = (cap.mime, cap.image)
        (self.out_dir / f"{cid}.jpg").write_bytes(cap.image)
        self.push(
            "capture",
            turn=self._turn_no,
            id=cid,
            number=e.number,
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

    @staticmethod
    def _cid(number: int | None, detail: str) -> str:
        return f"{number:03d}-{detail}"

    # ---- popovers (on the loop) ----------------------------------------------

    def show_popover(self, kind: str, title: str, payload: dict) -> str:
        """Opens a popover on the page and returns its id. Raises ValueError if `payload` doesn't suit `kind`."""
        if not kind or not isinstance(payload, dict):
            raise ValueError("a popover needs a kind and a payload object")
        pid = f"p{self._popover_no + 1}"
        prepare = POPOVER_KINDS.get(kind)
        payload = prepare(self, pid, title, payload) if prepare else payload
        self._popover_no += 1
        self._popovers.append(pid)
        self.push("popover", turn=self._turn_no, id=pid, kind=kind, title=title or kind, payload=payload)
        return pid

    def dismiss_popovers(self, which: str = "top") -> list[str]:
        """Closes popover `which` ("top": the one on top, "all", or an id); returns the ids closed."""
        if which == "all":
            ids, self._popovers = self._popovers, []
        elif which == "top":
            ids = self._popovers[-1:]
            del self._popovers[-1:]
        elif which in self._popovers:
            ids = [which]
            self._popovers.remove(which)
        else:
            ids = []
        for pid in ids:
            self._steps.pop(pid, None)
        if ids:
            self.push("popover_close", ids=ids)
        return ids

    def raise_popover(self, pid: str) -> None:
        """The page brought `pid` to the front (no event: other pages keep their own stacking)."""
        if pid in self._popovers:
            self._popovers.remove(pid)
            self._popovers.append(pid)

    def _annotated_image(self, pid: str, title: str, payload: dict) -> dict:
        number = payload.get("capture")
        cap = self._capture_log.get(number) if isinstance(number, int) else None
        if cap is None:
            raise ValueError(f"there is no capture {number}")
        annotations = parse_annotations(payload.get("annotations"))
        cid = self._cid(number, cap.detail)
        name, width, height = self._render(f"{cid}-{pid}.svg", cap, annotations)
        return {"capture": number, "source": cid, "annotations": annotations, "caption": str(payload.get("caption") or ""),
                "image": f"/api/session/files/{name}", "width": width, "height": height}

    def _steps_popover(self, pid: str, title: str, payload: dict) -> dict:
        """One SVG per step (its own marks, plus the earlier steps' in grey unless `show_earlier` is false),
        and {pid}-steps.json listing them, for review."""
        steps = parse_steps(payload.get("steps"), self._capture_log.latest)
        caps = []
        for n, s in enumerate(steps, 1):
            cap = self._capture_log.get(s["capture"])
            if cap is None:
                raise ValueError(f"step {n}: there is no capture {s['capture']}")
            caps.append(cap)
        show_earlier = payload.get("show_earlier", True) is not False
        out = []
        for n, (s, cap) in enumerate(zip(steps, caps), 1):
            earlier = [m for prev, pcap in zip(steps[:n - 1], caps) for m in earlier_marks(prev["annotations"], pcap, cap)
                       ] if show_earlier else []
            cid = self._cid(s["capture"], cap.detail)
            name, width, height = self._render(f"{cid}-{pid}-s{n}.svg", cap, earlier + s["annotations"])
            out.append({**s, "source": cid, "earlier": earlier, "image": f"/api/session/files/{name}",
                        "width": width, "height": height})
        (self.out_dir / f"{pid}-steps.json").write_text(json.dumps(
            {"id": pid, "turn": self._turn_no, "title": title, "show_earlier": show_earlier, "steps": out},
            indent=2))
        self._steps[pid] = {"steps": out, "at": 1}
        return {"steps": out, "step": 1, "show_earlier": show_earlier}

    def _render(self, name: str, cap: Capture, annotations: list[dict]) -> tuple[str, int, int]:
        """Draws `annotations` over `cap` into SVG file `name` (saved and served); returns (name, width, height)."""
        svg = render_svg(cap.image, cap.mime, cap.width, cap.height, annotations).encode()
        (self.out_dir / name).write_bytes(svg)
        self._popover_files[name] = ("image/svg+xml", svg)
        return name, cap.width, cap.height

    # ---- step groups (on the loop) ----------------------------------------------

    def step_popover(self, to: str | int, which: str = "top", *, by: str = "") -> StepMove | None:
        """Moves steps popover `which` (an id, or "top": the topmost steps popover) to step `to` ("next",
        "back", "repeat" or a number from 1), reading it out if speech is on. None if there is no such popover.

        Moving past either end stays put (`moved` is false). Every move but that is a "popover_step" event."""
        pid = self._top_steps() if which == "top" else which if which in self._steps else None
        if pid is None:
            return None
        group = self._steps[pid]
        total, at = len(group["steps"]), group["at"]
        if to == "next":
            step = at + 1
        elif to == "back":
            step = at - 1
        elif to == "repeat":
            step = at
        elif isinstance(to, int) and not isinstance(to, bool):
            step = to
        else:
            raise ValueError(f"can't go to step {to!r}")
        if not 1 <= step <= total:
            return StepMove(pid, at, total, moved=False)
        group["at"] = step
        self.push("popover_step", id=pid, step=step, total=total, **({"by": by} if by else {}))
        s = group["steps"][step - 1]
        if self.voice:
            self.voice.read_out(f"Step {step}. {s['say']}")
        return StepMove(pid, step, total, moved=True)

    def _top_steps(self) -> str | None:
        return next((p for p in reversed(self._popovers) if p in self._steps), None)

    def _on_screen(self) -> str | None:
        """The step showing in the topmost steps popover, for the agent."""
        if (pid := self._top_steps()) is None:
            return None
        group = self._steps[pid]
        at, total = group["at"], len(group["steps"])
        last = " (the last)" if at == total else ""
        return f"[On the user's screen: steps popover {pid}, at step {at} of {total}{last}: {group['steps'][at - 1]['title']}.]"

    # ---- readers -------------------------------------------------------------

    def image(self, cid: str) -> tuple[str, bytes] | None:
        return self._captures.get(cid)

    def file(self, name: str) -> tuple[str, bytes] | None:
        """A rendered popover file (annotated capture), by name."""
        return self._popover_files.get(name)

    def events_after(self, seq: int, wait: float = 0) -> list[dict]:
        """Events with a higher seq; blocks up to `wait` seconds for one to arrive."""
        with self._changed:
            if wait and (not self._events or self._events[-1]["seq"] <= seq):
                self._changed.wait(wait)
            return [e for e in self._events[-500:] if e["seq"] > seq]

    def summary(self) -> dict:
        return {"state": self.state, "error": self.error, "cost_usd": self.cost_usd, "model": self.model,                "out_dir": str(self.out_dir), "budget": self.budget.state(),
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


POPOVER_KINDS = {
    "annotated-image": LiveSession._annotated_image,
    "steps": LiveSession._steps_popover,
}
"""Per-kind step, (session, id, title, payload) -> payload, that checks a popover's payload and adds what the page
needs to show it (kinds not listed pass through)."""
