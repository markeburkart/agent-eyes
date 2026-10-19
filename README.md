# Agent Eyes

Hands-free voice and vision assistant for the workbench.

Needs macOS 14+, Xcode command line tools (`swift`) and [uv](https://docs.astral.sh/uv/).

## Run

```sh
uv run agent-eyes                                  # camera, agent and dashboard together; opens the browser
uv run agent-eyes --per-turn 1 --per-minute 4      # start with a tighter capture budget
uv run agent-eyes --allow-builtin --front-camera "MacBook Pro Camera"   # dry run without the phone
uv run agent-eyes --url http://127.0.0.1:PORT      # attach to a camsvc that's already running
```

One camera for now: the phone's main camera ("front"), on an arm looking down at the work area.
The dashboard shows:

- the conversation: type to the agent (Enter sends, Stop interrupts a turn), with each look,
  turn time and the conversation's running cost
- the live camera view, with the region the agent just looked at outlined, and the camera's
  state: live, reconnecting (no fresh frames) or not available
- a filmstrip of the agent's captures with its reason and crop region, including failed looks
  and why; click one to enlarge it
- the capture budget: what's used this turn and in the last minute, +/- for both limits, and a
  switch to stop the agent looking at all

Each run writes `transcript.jsonl` and the capture images to `captures/sessions/<start time>/`.
"New conversation" starts the agent over. The camera diagnostics page (`/cameras`) is still
there for previews, helper events and manual captures. Ctrl-C (or closing the terminal) stops the
app, the agent and the camera helper. The first run asks for camera access for your terminal app.

If the camera shows as not available, the phone isn't offered to the Mac: keep it near the Mac,
locked, on the same Apple ID, with Wi-Fi, Bluetooth and Continuity Camera on. When the phone drops
there's no disconnect event; the helper notices the missing frames and reopens the stream, which
takes about 3–8 s. A look during that time fails with `no_frames`, and the agent says so.

Manual camera checks without the browser:

```sh
uv run agent-eyes camera list
uv run agent-eyes camera capture --view desk --detail inspect --region 0.4,0.4,0.2,0.2 --out cap.jpg
```

Talk to the agent by typing (one line per turn; Ctrl-D to quit). It uses your Claude Code login
and decides on its own when to look through the camera. Each capture is printed with the
agent's reason and saved under `captures/chat/`.

```sh
uv run agent-eyes chat                               # up to 3 captures per turn, 10 per minute
uv run agent-eyes chat --per-turn 1 --per-minute 4   # tighter capture budget
```

## Layout

```
src/agent_eyes/
  cli.py           the agent-eyes command
  agent/           Claude Agent SDK session (session.py), capture_frame tool (tools.py), capture budget (budget.py)
  camera/          camera abstraction (base.py) and the camsvc-backed source (continuity.py)
  web/             local web server (server.py), the agent session behind the dashboard (live.py), pages (static/)
camera-service/    camsvc: Swift helper that owns the cameras (built automatically on launch)
spike/             Phase 1 Continuity Camera capability probe
```
