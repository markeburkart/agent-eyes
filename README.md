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
- the voice state in a strip across the top, large enough to read from the bench: say "hey eyes",
  listening, agent answering, or conversation live with the seconds left before it closes (the
  tab title shows "● Agent Eyes: conversation live" too). "End conversation" closes it (or drops a
  request being dictated), like saying "ok thanks". Its switches turn voice commands and
  reading replies out on and off; spoken requests show in the conversation with a 🎙. Its voice
  dropdown picks the voice replies are read out with (see [Voices](#voices))
- popovers: when something is easier to show than to say, the agent marks up one of its captures
  (boxes, circles, arrows and short labels) and it opens in a popover over the dashboard. Drag a
  popover by its title bar; clicking one brings it to the front; × closes it. Several can be open
  at once. Say "dismiss" (or "dismiss that") to close the one on top (the newest or the one
  clicked last), or "dismiss all" (or "dismiss everything") to close them all; like any request,
  that needs "hey eyes" unless the conversation is live. New conversation closes them too. Each
  annotated capture is saved as an SVG next to the capture (`003-inspect-p2.svg`), and its marks
  are in `transcript.jsonl`
- steps: for instructions with several physical steps (wiring power, ground and two signal pins,
  then setting a switch) the agent opens one popover you step through, starting at step 1: one
  marked-up capture per step, marking only that step, with the earlier steps' marks in grey. Move
  with Back / Next, the step numbers or the arrow keys, or say "next" (or "done"), "back" or
  "repeat"; each step is read out as you get to it. With no steps to move through ("next" on the
  last one, or none open), those words go to the agent like any request. While steps are open the
  agent knows which one is showing, so "is this right?" is about that step. Each step's SVG is
  saved (`002-inspect-p1-s3.svg`) with `p1-steps.json` listing the group
- New conversation (the agent forgets everything said so far; or say "hey eyes, new
  conversation", "start over" or "clear context") and Shut down (stops the agent, voice and
  camera helper and exits, like Ctrl-C)

`uv run agent-eyes --voice` starts with voice on (same options as `chat --voice` below, e.g.
`--wake-word`, `--follow-up`, `--no-speech`, `--speech-voice`).

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

Hands-free: add `--voice` and say the wake word, then your request. The request ends when you
pause (1.5 s by default). Saying "hey eyes", waiting for the chime, then the request also works.
To drop a request, end it with "never mind". Typing keeps working alongside. Speech is
transcribed on the Mac by Apple's on-device model (macOS 26+). The first run downloads the
model and asks for microphone access for your terminal app.

After a spoken request the conversation stays live: follow-ups ("like this?") need no wake word.
It closes after 30 s without anyone speaking (the agent's own thinking time doesn't count), or
when you say "ok thanks" (or "that's all"). While it is live the terminal tab title shows
"● agent-eyes: conversation live", and a sound plays when it closes. Closing only means the
wake word is needed again; the agent keeps the conversation's context.

Replies are read out with the Mac's voice (`--no-speech` for text only; `--speech` without
`--voice` to hear replies to typed requests). The mic stays on while the agent speaks, but its
own words are filtered out of what the mic hears, so requests pause during the readout; say
"stop" or "ok" to cut it short, or "ok thanks" to cut it short and close the conversation.
Say "slow down" (or "slower") to have it speak more slowly from then on: it goes back to the
start of the sentence it was reading and carries on at the slower pace. Each "slow down" slows it
a bit more, until the next start. Said on its own as a request, it just slows the next replies.

Instructions with several steps come one step per reply: the agent says how many steps there
are, gives the first, and waits for you to say "next" or "done" before the next one ("repeat"
says the current one again). If a step takes longer than the 30 s follow-up window, start with the
wake word: "hey eyes, next".

Both can be switched during a chat by typing `/voice` (voice commands; off closes the mic) and
`/speech` (reading replies out; off also stops a readout in progress), or `/voice on|off`,
`/speech on|off`.

### Voices

Replies are read out with the best voice installed for the speech locale: Premium, then
Enhanced, then compact (the novelty voices and the robotic Eloquence ones, Eddy, Grandma etc.,
only when chosen). The compact voices macOS comes with sound mechanical; for a natural one, open
System Settings → Accessibility → Spoken Content → Manage Voices and download e.g. Ava (Premium),
Zoe (Premium) or Evan (Enhanced) (Siri voices can't be used: they aren't offered to apps).

Pick one in the dashboard's voice dropdown (next to the voice switches, best voices first,
labelled with their quality, e.g. "Ava — Premium"): it applies to the next thing read out, says a
short sample ("This is Ava.") and is remembered for the next run, in
`~/.config/agent-eyes/settings.json` (the chat uses it too). Without any Premium or Enhanced voice
installed, the dashboard points to where to get one; reload the page after downloading.
`--speech-voice NAME` picks one for a single run, by name (`Ava`, `"Zoe (Premium)"`) or identifier
(`com.apple.voice.premium.en-US.Ava`). (`--voice` already switches voice commands on.)

```sh
uv run agent-eyes chat --voice                         # "hey eyes, is the red wire on pin five?"
uv run agent-eyes chat --voice --wake-word "ok bench"  # your own wake phrase(s), comma-separated
uv run agent-eyes chat --voice --pause 2.5             # longer pause before a request is sent
uv run agent-eyes chat --voice --follow-up 60          # keep conversations open longer (0 = wake word every time)
uv run agent-eyes chat --voice --speech-voice Zoe      # read out with Zoe this time
```

## Layout

```
src/agent_eyes/
  cli.py           the agent-eyes command
  agent/           Claude Agent SDK session (session.py), capture_frame, annotate_capture and show_steps tools
                   (tools.py), annotations rendered to SVG (annotate.py), steps (steps.py), capture budget (budget.py)
  camera/          camera abstraction (base.py) and the camsvc-backed source (continuity.py)
  settings.py      choices that last across runs (the speaking voice), ~/.config/agent-eyes/settings.json
  voice/           voicesvc (helper.py, also lists the installed voices), wake word + dictation and spoken actions (commands.py), readout echo filter (echo.py)
  web/             local web server (server.py), the agent session behind the dashboard (live.py), pages (static/)
camera-service/    camsvc: Swift helper that owns the cameras (built automatically on launch)
voice-service/     voicesvc: Swift helper for on-device transcription and speech (built when voice is first used)
spike/             Phase 1 Continuity Camera capability probe
```

## Extending

- Voice commands: the spoken actions ("new conversation", "dismiss", "dismiss all", "next", ...) are the
  `ACTIONS` table in `voice/commands.py`, phrases by name. Add an entry there and a handler with
  the same name in `LiveVoice.actions` (`web/voice.py`); edit the phrases to change what's said.
- Popovers: the page draws each kind of popover with a renderer in `POPOVER_RENDERERS`
  (`web/static/agent.html`); the window around it (drag, ×, stacking, "dismiss") is the same for
  all. Open one with `LiveSession.show_popover(kind, title, payload)` or
  `POST /api/session/popovers`. If the server must check or prepare the payload first (as
  `annotated-image` does, rendering the SVG), add a step to `POPOVER_KINDS` in `web/live.py`.
