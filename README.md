# Agent Eyes

Hands-free voice and vision assistant for the workbench.

Needs macOS 14+, Xcode command line tools (`swift`) and [uv](https://docs.astral.sh/uv/).

## Run

```sh
uv run agent-eyes                    # opens the app in the browser
uv run agent-eyes --allow-builtin    # also show the Mac's own cameras
uv run agent-eyes --url http://127.0.0.1:PORT   # attach to a camsvc that's already running
```

The app currently shows the camera views: live previews, per-camera status (streaming,
suspended, frame age), helper events (disconnects, stalls, reopens), and overview/inspect
captures with region, sharpness and candidate scores. Ctrl-C (or closing the terminal) stops
the app and the camera helper. The first run asks for camera access for your terminal app.

Manual camera checks without the browser:

```sh
uv run agent-eyes camera list
uv run agent-eyes camera capture --view desk --detail inspect --region 0.4,0.4,0.2,0.2 --out cap.jpg
```

## Layout

```
src/agent_eyes/
  cli.py           the agent-eyes command
  camera/          camera abstraction (base.py) and the camsvc-backed source (continuity.py)
  web/             local web server (server.py) and browser views (static/)
camera-service/    camsvc: Swift helper that owns the cameras (built automatically on launch)
spike/             Phase 1 Continuity Camera capability probe
```
