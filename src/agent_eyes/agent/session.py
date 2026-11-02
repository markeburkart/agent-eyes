"""The Agent Eyes conversation: one long-lived Claude Agent SDK session with the camera tool."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock

from ..camera import CameraSource
from .budget import CaptureBudget
from .tools import ANNOTATE_TOOL, CAPTURE_TOOL, SERVER_NAME, CaptureEvent, CaptureLog, ShowPopover, capture_server

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """\
You are Agent Eyes, a hands-free assistant at the user's electronics workbench. The user is \
working with their hands, so your replies are spoken aloud.

How to talk:
- Keep replies short: one to three sentences.
- Plain spoken language only. No markdown, lists, headings, code blocks or emoji.
- Give one step at a time, and confirm the user is ready before moving on.
- Say numbers and pin names the way a person would say them out loud.

When to look:
- You can see the bench with the capture_frame tool, but every look costs time and money.
- Look only when the answer depends on the current physical state of the bench (what is \
connected, where a wire goes, whether a joint looks good), or when the user asks you to look \
or check something.
- Don't look for general knowledge questions, conversation, or things the user just told you.
- There is one camera, "front": the user's phone on an arm, looking down at the work area.
- Start with an overview. To read small parts, call inspect with a region taken from that \
overview, as fractions of the frame from the top-left.
- If a capture fails or you are over the capture budget, say so briefly instead of guessing \
what the bench looks like.
"""

POPOVER_PROMPT = """
Showing the user:
- To point something out or show where to do something, mark up one of your captures with \
annotate_capture: boxes, circles, arrows and short labels. It appears in a popover on the \
user's screen. Coordinates are fractions of that capture's own image from the top-left; for an \
inspect, that's the region you inspected, not the whole frame.
- Use it when a location is easier to show than to say. Still say briefly what you marked, e.g. \
"I've circled the pin on the screen."
"""


@dataclass(frozen=True)
class TurnResult:
    text: str
    cost_usd: float | None
    """Running total for the whole session so far, as the SDK reports it."""
    duration_ms: int
    is_error: bool


class EyesAgent:
    """Send the user's words with `ask()`; capture_frame calls are reported through `on_capture`, annotations
    go to `show_popover`."""

    def __init__(
        self,
        camera: CameraSource,
        budget: CaptureBudget | None = None,
        *,
        model: str = DEFAULT_MODEL,
        effort: str | None = "low",
        on_capture: Callable[[CaptureEvent], None] = lambda event: None,
        captures: CaptureLog | None = None,
        show_popover: ShowPopover | None = None,
    ):
        """`show_popover`: there's a screen (the dashboard) to show things on; adds annotate_capture.
        `captures`: keeps capture numbers going across conversations."""
        self.budget = budget or CaptureBudget()
        self.captures = captures or CaptureLog()
        self._client = ClaudeSDKClient(
            ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT + (POPOVER_PROMPT if show_popover else ""),
                model=model,
                effort=effort,
                # No built-in Claude Code tools (Bash, Read, ...): the agent only gets the camera (and the screen).
                tools=[],
                mcp_servers={SERVER_NAME: capture_server(camera, self.budget, on_capture, self.captures, show_popover)},
                allowed_tools=[CAPTURE_TOOL] + ([ANNOTATE_TOOL] if show_popover else []),
                # Don't pick up the developer's own CLAUDE.md, skills or settings.
                setting_sources=[],
            )
        )

    async def __aenter__(self) -> EyesAgent:
        await self._client.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.disconnect()

    async def ask(self, text: str) -> AsyncIterator[str | TurnResult]:
        """Yields the agent's text as it arrives, then one TurnResult."""
        self.budget.start_turn()
        await self._client.query(text)
        said: list[str] = []
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        said.append(block.text.strip())
                        yield block.text.strip()
            elif isinstance(msg, ResultMessage):
                yield TurnResult(" ".join(said), msg.total_cost_usd, msg.duration_ms, msg.is_error)

    async def interrupt(self) -> None:
        await self._client.interrupt()
