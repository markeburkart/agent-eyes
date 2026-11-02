"""Popovers on the dashboard: the agent's annotations (checked and rendered to SVG), opening them
through the session, and closing them (×, "dismiss", "dismiss all", new conversation); steps
popovers (one image per step, earlier steps greyed) and moving through them ("next", "back", "repeat").

Run with: uv run python -m unittest discover tests
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from agent_eyes.agent import CaptureBudget, CaptureLog
from agent_eyes.agent.annotate import COLORS, parse_annotations, render_svg
from agent_eyes.agent.steps import EARLIER_COLOR, earlier_marks, parse_steps
from agent_eyes.camera import Capture, Region
from agent_eyes.camera.base import CameraInfo
from agent_eyes.web.live import LiveSession
from agent_eyes.web.voice import LiveVoice, VoiceOptions


def capture(detail: str = "overview", width: int = 640, height: int = 480, region: Region | None = None,
            camera: str = "cam") -> Capture:
    return Capture(image=b"\xff\xd8 fake jpeg", mime="image/jpeg", width=width, height=height,
                   camera=CameraInfo(camera, "Test Camera", "built_in"), detail=detail, reason="test",
                   region=region, source_size=(width, height), captured_at=datetime.now())


BOX = {"shape": "box", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "label": "pin 5"}


class ParseAnnotationsTest(unittest.TestCase):
    def test_shapes(self):
        got = parse_annotations([
            BOX,
            {"shape": "circle", "x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1, "color": "red"},
            {"shape": "arrow", "x1": 0.9, "y1": 0.9, "x2": 0.6, "y2": 0.6, "label": "here"},
            {"shape": "text", "x": 0.05, "y": 0.05, "label": "GND", "color": "#00ff00"},
        ])
        self.assertEqual(got[0], {**BOX, "color": COLORS["yellow"]})
        self.assertEqual(got[1]["color"], COLORS["red"])
        self.assertEqual(got[3]["color"], "#00ff00")

    def test_box_is_clipped_to_the_image(self):
        got = parse_annotations([{"shape": "box", "x": 0.8, "y": 0.9, "w": 0.5, "h": 0.5}])[0]
        self.assertAlmostEqual(got["w"], 0.2)
        self.assertAlmostEqual(got["h"], 0.1)

    def test_bad_annotations(self):
        for bad in ([], None, [{"shape": "star"}], [{"shape": "box", "x": 0.1, "y": 0.1, "w": 0.2}],
                    [{"shape": "box", "x": 1.5, "y": 0, "w": 0.1, "h": 0.1}], [{"shape": "text", "x": 0, "y": 0}],
                    [{**BOX, "color": "url(#x)"}], [{**BOX, "x": "left"}], [BOX] * 13):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_annotations(bad)


class RenderSvgTest(unittest.TestCase):
    def test_svg(self):
        anns = parse_annotations([BOX, {"shape": "arrow", "x1": 0.9, "y1": 0.9, "x2": 0.6, "y2": 0.6},
                                  {"shape": "text", "x": 0.1, "y": 0.1, "label": "<b>&"}])
        svg = render_svg(b"img", "image/jpeg", 640, 480, anns)
        self.assertIn('viewBox="0 0 640 480"', svg)
        self.assertIn('href="data:image/jpeg;base64,aW1n"', svg)
        self.assertIn('<rect x="64.0" y="96.0" width="192.0" height="192.0"', svg)
        self.assertIn(">pin 5</text>", svg)
        self.assertIn("&lt;b&gt;&amp;", svg)  # labels are escaped
        self.assertIn("<polygon", svg)


class PopoverSessionTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.session = LiveSession(None, CaptureBudget(), self.dir, model="test")
        self.log: CaptureLog = self.session._capture_log
        self.log.add(capture())
        self.log.add(capture("inspect", 300, 200))

    def tearDown(self):
        self.session._log.close()

    def events(self, type: str) -> list[dict]:
        return [e for e in self.session.events_after(0) if e["type"] == type]

    def test_annotated_image(self):
        pid = self.session.show_popover("annotated-image", "The red wire", {"capture": 2, "annotations": [BOX],
                                                                           "caption": "goes to pin 5"})
        [e] = self.events("popover")
        self.assertEqual((e["id"], e["kind"], e["title"]), (pid, "annotated-image", "The red wire"))
        p = e["payload"]
        self.assertEqual((p["capture"], p["source"], p["width"], p["height"], p["caption"]), (2, "002-inspect", 300, 200, "goes to pin 5"))
        name = p["image"].rsplit("/", 1)[1]
        self.assertEqual(name, f"002-inspect-{pid}.svg")
        mime, svg = self.session.file(name)
        self.assertEqual(mime, "image/svg+xml")
        self.assertEqual((self.dir / name).read_bytes(), svg)  # saved next to the captures
        logged = [json.loads(line) for line in (self.dir / "transcript.jsonl").read_text().splitlines()]
        self.assertEqual(logged[-1]["payload"]["annotations"][0]["label"], "pin 5")

    def test_bad_payload_opens_nothing(self):
        for payload in ({"capture": 9, "annotations": [BOX]}, {"capture": 1, "annotations": []}, {"annotations": [BOX]}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.session.show_popover("annotated-image", "x", payload)
        self.assertEqual(self.events("popover"), [])

    def test_other_kinds_pass_through(self):
        pid = self.session.show_popover("markdown", "Notes", {"text": "hello"})
        self.assertEqual(self.events("popover")[0]["payload"], {"text": "hello"})
        self.assertEqual(pid, "p1")

    def open(self, n: int) -> list[str]:
        return [self.session.show_popover("annotated-image", f"#{i}", {"capture": 1, "annotations": [BOX]}) for i in range(n)]

    def test_dismiss_closes_the_top_one(self):
        p1, p2, p3 = self.open(3)
        self.assertEqual(self.session.dismiss_popovers(), [p3])
        self.session.raise_popover(p1)  # clicked on the page
        self.assertEqual(self.session.dismiss_popovers("top"), [p1])
        self.assertEqual(self.session.dismiss_popovers("top"), [p2])
        self.assertEqual(self.session.dismiss_popovers("top"), [])
        self.assertEqual([e["ids"] for e in self.events("popover_close")], [[p3], [p1], [p2]])

    def test_dismiss_all_and_by_id(self):
        p1, p2, p3 = self.open(3)
        self.assertEqual(self.session.dismiss_popovers(p2), [p2])  # the × button
        self.assertEqual(self.session.dismiss_popovers(p2), [])
        self.assertEqual(self.session.dismiss_popovers("all"), [p1, p3])
        self.assertEqual(self.session.dismiss_popovers("all"), [])

    @mock.patch("agent_eyes.web.voice.chime")
    def test_voice_dismiss(self, chime):
        voice = LiveVoice(self.session, VoiceOptions())
        p1, p2 = self.open(2)
        voice.actions["dismiss"]("dismiss")
        voice.actions["dismiss_all"]("dismiss all")
        voice.actions["dismiss_all"]("dismiss all")
        self.assertEqual([e["ids"] for e in self.events("popover_close")], [[p2], [p1]])
        self.assertEqual([e["text"] for e in self.events("voice")][-3:],
                         ["dismissed 1 popover", "dismissed 1 popover", "no popover to dismiss"])
        self.assertEqual([c.args[0] for c in chime.call_args_list], ["Pop", "Pop", "Bottle"])


CIRCLE = {"shape": "circle", "x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1, "label": "GND"}


def step(title: str, *annotations: dict, **extra) -> dict:
    return {"title": title, "say": f"Do {title}.", "annotations": list(annotations), **extra}


class ParseStepsTest(unittest.TestCase):
    def test_steps(self):
        got = parse_steps([step("Power", BOX), step("Ground", CIRCLE, capture=1), step("Switch")], latest=2)
        self.assertEqual([s["capture"] for s in got], [2, 1, 2])  # the latest capture unless given
        self.assertEqual(got[0]["annotations"][0]["color"], COLORS["yellow"])
        self.assertEqual(got[2]["annotations"], [])  # a step may have nothing to mark

    def test_bad_steps(self):
        for bad, why in (([step("Only", BOX)], "at least 2"), (None, "at least 2"), ([step("A"), "B"], "step 2"),
                         ([step("A"), {"title": "B", "annotations": []}], "step 2 needs"),
                         ([step("A"), step("B", {"shape": "star"})], "step 2: annotation 1"),
                         ([step("A"), step("B", capture="first")], "step 2: capture"), ([step("A")] * 13, "at most")):
            with self.subTest(why=why):
                with self.assertRaisesRegex(ValueError, why):
                    parse_steps(bad, latest=1)
        with self.assertRaisesRegex(ValueError, "capture"):
            parse_steps([step("A"), step("B")], latest=None)


class EarlierMarksTest(unittest.TestCase):
    def test_same_capture_is_greyed_without_labels(self):
        cap = capture()
        [m] = earlier_marks(parse_annotations([BOX]), cap, cap)
        self.assertEqual(m, {"shape": "box", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "color": EARLIER_COLOR})

    def test_moved_onto_an_inspect_of_a_region(self):
        overview, crop = capture(), capture("inspect", region=Region(0.5, 0.5, 0.5, 0.5))
        marks = parse_annotations([
            {"shape": "circle", "x": 0.6, "y": 0.6, "w": 0.1, "h": 0.1},  # inside the crop
            {"shape": "box", "x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},  # half in: cut to it
            {"shape": "arrow", "x1": 0.1, "y1": 0.1, "x2": 0.7, "y2": 0.7},  # tail outside: dropped
            {"shape": "text", "x": 0.6, "y": 0.6, "label": "VCC"},  # only a label: dropped
        ])
        got = earlier_marks(marks, overview, crop)
        self.assertEqual([m["shape"] for m in got], ["circle", "box"])
        for k, v in {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}.items():
            self.assertAlmostEqual(got[0][k], v)
        for k, v in {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2}.items():
            self.assertAlmostEqual(got[1][k], v)
        # And back: the crop's marks onto the overview.
        [back] = earlier_marks(parse_annotations([{"shape": "box", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}]), crop, overview)
        self.assertAlmostEqual(back["x"], 0.6)
        self.assertAlmostEqual(back["w"], 0.1)

    def test_other_camera(self):
        self.assertEqual(earlier_marks(parse_annotations([BOX]), capture(), capture(camera="other")), [])


class StepsPopoverTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.session = LiveSession(None, CaptureBudget(), self.dir, model="test")
        log = self.session._capture_log
        log.add(capture())
        log.add(capture("inspect", 320, 240, region=Region(0.5, 0.5, 0.5, 0.5)))

    def tearDown(self):
        self.session._log.close()

    def events(self, type: str) -> list[dict]:
        return [e for e in self.session.events_after(0) if e["type"] == type]

    def open(self, **extra) -> str:
        return self.session.show_popover("steps", "Wire it", {"steps": [
            step("Power", {"shape": "circle", "x": 0.6, "y": 0.6, "w": 0.1, "h": 0.1, "label": "VCC"}, capture=1),
            step("Ground", BOX, capture=1),
            step("Switch", CIRCLE, capture=2),
        ], **extra})

    def test_one_image_per_step(self):
        pid = self.open()
        [e] = self.events("popover")
        p = e["payload"]
        self.assertEqual((e["kind"], e["title"], p["step"], len(p["steps"])), ("steps", "Wire it", 1, 3))
        s1, s2, s3 = p["steps"]
        self.assertEqual([s["image"].rsplit("/", 1)[1] for s in p["steps"]],
                         [f"001-overview-{pid}-s1.svg", f"001-overview-{pid}-s2.svg", f"002-inspect-{pid}-s3.svg"])
        self.assertEqual((s1["earlier"], len(s2["earlier"]), len(s3["earlier"])), ([], 1, 1))  # step 2's box falls outside the crop
        self.assertEqual(s3["earlier"][0]["shape"], "circle")
        self.assertEqual((s3["width"], s3["height"], s3["source"]), (320, 240, "002-inspect"))
        svg2 = self.session.file(f"001-overview-{pid}-s2.svg")[1].decode()
        self.assertIn(EARLIER_COLOR, svg2)
        self.assertIn(">pin 5</text>", svg2)
        self.assertNotIn(">VCC</text>", svg2)  # earlier steps' labels are left out
        manifest = json.loads((self.dir / f"{pid}-steps.json").read_text())
        self.assertEqual((manifest["title"], [s["title"] for s in manifest["steps"]]), ("Wire it", ["Power", "Ground", "Switch"]))
        for s in p["steps"]:
            self.assertTrue((self.dir / s["image"].rsplit("/", 1)[1]).exists())

    def test_without_earlier_steps(self):
        self.open(show_earlier=False)
        self.assertEqual([s["earlier"] for s in self.events("popover")[0]["payload"]["steps"]], [[], [], []])

    def test_bad_steps_open_nothing(self):
        for steps in ([step("A", BOX)], [step("A", BOX), step("B", BOX, capture=9)]):
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError):
                    self.session.show_popover("steps", "x", {"steps": steps})
        self.assertEqual((self.events("popover"), self.session._steps), ([], {}))

    def test_moving_through_the_steps(self):
        pid = self.open()
        move = self.session.step_popover
        self.assertEqual((move("back").step, move("back").moved), (1, False))
        self.assertEqual([move("next").step, move("next").step, move("repeat").step], [2, 3, 3])
        self.assertEqual((move("next").step, move("next").moved), (3, False))
        self.assertEqual(move(1, pid, by="page").step, 1)
        with self.assertRaises(ValueError):
            move("sideways")
        self.assertEqual([(e["step"], e.get("by")) for e in self.events("popover_step")],
                         [(2, None), (3, None), (3, None), (1, "page")])

    def test_voice_moves_the_top_steps_popover(self):
        first, second = self.open(), self.open()
        self.session.show_popover("annotated-image", "x", {"capture": 1, "annotations": [BOX]})  # not steps
        self.assertEqual(self.session.step_popover("next").id, second)
        self.session.raise_popover(first)
        self.assertEqual(self.session.step_popover("next").id, first)
        self.session.dismiss_popovers("all")
        self.assertIsNone(self.session.step_popover("next"))
        self.assertIsNone(self.session.step_popover(2, first))

    def test_the_agent_hears_which_step_is_showing(self):
        self.assertIsNone(self.session._on_screen())
        pid = self.open()
        self.assertEqual(self.session._on_screen(), f"[On the user's screen: steps popover {pid}, at step 1 of 3: Power.]")
        self.session.step_popover(3)
        self.assertIn("at step 3 of 3 (the last): Switch.", self.session._on_screen())
        self.session.dismiss_popovers()
        self.assertIsNone(self.session._on_screen())


class StepsVoiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = LiveSession(None, CaptureBudget(), Path(tempfile.mkdtemp()), model="test")
        self.session._capture_log.add(capture())
        self.voice = self.session.voice = LiveVoice(self.session, VoiceOptions())
        self.voice.read_out = mock.Mock()
        self.asked = []

        async def ask(text, *, voice=False):
            self.asked.append(text)
        self.session.ask = ask

    async def asyncTearDown(self):
        self.session._log.close()

    async def say(self, action: str, said: str) -> None:
        if (pending := self.voice.actions[action](said)) is not None:
            await pending

    @mock.patch("agent_eyes.web.voice.chime")
    async def test_steps(self, chime):
        await self.say("next_step", "done")  # no steps open: it's for the agent
        self.session.show_popover("steps", "Wire it", {"steps": [step("Power", BOX), step("Ground", CIRCLE)]})
        await self.say("previous_step", "back")
        await self.say("next_step", "next")
        await self.say("repeat_step", "say again")
        await self.say("next_step", "done")  # past the last step: for the agent too
        self.assertEqual(self.asked, ["done", "done"])
        self.assertEqual([c.args[0] for c in self.voice.read_out.call_args_list], ["Step 2. Do Ground."] * 2)
        self.assertIn("already at step 1 of 2", [e.get("text") for e in self.session.events_after(0)])
        self.assertEqual([c.args[0] for c in chime.call_args_list], ["Pop", "Bottle", "Pop", "Pop", "Pop"])


class NewConversationTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_conversation_closes_popovers(self):
        session = LiveSession(None, CaptureBudget(), Path(tempfile.mkdtemp()), model="test")
        session._capture_log.add(capture())
        session.state = "ready"
        pid = session.show_popover("annotated-image", "x", {"capture": 1, "annotations": [BOX]})

        async def no_agent():
            pass
        session.start = no_agent
        await session.restart()
        self.assertIn({"ids": [pid]}, [{"ids": e["ids"]} for e in session.events_after(0) if e["type"] == "popover_close"])
        self.assertEqual(session.dismiss_popovers("all"), [])
        session._log.close()


if __name__ == "__main__":
    unittest.main()
