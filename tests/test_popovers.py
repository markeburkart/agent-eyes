"""Popovers on the dashboard: the agent's annotations (checked and rendered to SVG), opening them
through the session, and closing them (×, "dismiss", "dismiss all", new conversation).

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
from agent_eyes.camera import Capture
from agent_eyes.camera.base import CameraInfo
from agent_eyes.web.live import LiveSession
from agent_eyes.web.voice import LiveVoice, VoiceOptions


def capture(detail: str = "overview", width: int = 640, height: int = 480) -> Capture:
    return Capture(image=b"\xff\xd8 fake jpeg", mime="image/jpeg", width=width, height=height,
                   camera=CameraInfo("cam", "Test Camera", "built_in"), detail=detail, reason="test",
                   region=None, source_size=(width, height), captured_at=datetime.now())


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
        voice.actions["dismiss"]()
        voice.actions["dismiss_all"]()
        voice.actions["dismiss_all"]()
        self.assertEqual([e["ids"] for e in self.events("popover_close")], [[p2], [p1]])
        self.assertEqual([e["text"] for e in self.events("voice")][-3:],
                         ["dismissed 1 popover", "dismissed 1 popover", "no popover to dismiss"])
        self.assertEqual([c.args[0] for c in chime.call_args_list], ["Pop", "Pop", "Bottle"])


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
