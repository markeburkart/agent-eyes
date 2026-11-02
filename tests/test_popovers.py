"""Popovers on the dashboard: the agent's annotations (checked and rendered to SVG), opening them
through the session, and closing them (×, "dismiss", "dismiss all", new conversation).

Run with: uv run python -m unittest discover tests
"""

import json
import re
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
        r = render_svg(b"img", "image/jpeg", 640, 480, anns)
        self.assertEqual(r.width, 640)
        self.assertGreater(r.height, 480)  # room for the labels above the photo
        self.assertIn(f'viewBox="0 0 640 {r.height}"', r.svg)
        self.assertIn('href="data:image/jpeg;base64,aW1n"', r.svg)
        self.assertIn('<rect x="64.0" y="96.0" width="192.0" height="192.0"', r.svg)
        self.assertIn(">pin 5</text>", r.svg)
        self.assertIn("&lt;b&gt;&amp;", r.svg)  # labels are escaped
        self.assertIn("<polygon", r.svg)

    def test_no_labels_no_bands(self):
        r = render_svg(b"img", "image/jpeg", 640, 480, parse_annotations([{**BOX, "label": ""}]))
        self.assertEqual((r.width, r.height), (640, 480))

    def test_labels_go_beside_the_photo(self):
        # Close-together pins, as in a real capture where every label used to land on the same spot.
        anns = parse_annotations([
            {"shape": "circle", "x": 0.552, "y": 0.085, "w": 0.022, "h": 0.04, "label": "SDA to GP4 (pin 6)"},
            {"shape": "circle", "x": 0.538, "y": 0.13, "w": 0.022, "h": 0.04},
            {"shape": "text", "x": 0.43, "y": 0.02, "label": "SCL to GP5 (pin 7, magenta)"},
            {"shape": "circle", "x": 0.524, "y": 0.085, "w": 0.022, "h": 0.04, "label": "GND (pin 8)"},
            {"shape": "box", "x": 0.575, "y": 0.61, "w": 0.025, "h": 0.12, "label": "SCL SDA VCC GND header"},
            {"shape": "box", "x": 0.535, "y": 0.57, "w": 0.04, "h": 0.05, "label": "Mode switch: set to I2C"},
            {"shape": "arrow", "x1": 0.1, "y1": 0.9, "x2": 0.2, "y2": 0.8, "label": "from here"},
        ])
        W, H = 1280, 720
        r = render_svg(b"img", "image/jpeg", W, H, anns)
        # Chips, in the photo's coordinates (the group is moved down by the top band).
        chips = [tuple(map(float, c)) for c in
                 re.findall(r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)" rx="[\d.]+" fill="#', r.svg)]
        chips = [(x, y, x + w, y + h) for x, y, w, h in chips]
        self.assertEqual(len(chips), 6)
        top = float(re.search(r'translate\(0 ([\d.]+)\)', r.svg)[1])
        for i, a in enumerate(chips):
            self.assertTrue(0 <= a[0] and a[2] <= W + 0.1, a)
            self.assertTrue(a[3] <= 0 or a[1] >= H, f"{a} is on the photo")
            self.assertTrue(-top <= a[1] and a[3] <= r.height - top + 0.1, f"{a} is off the picture")
            for b in chips[i + 1:]:
                self.assertTrue(a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b))

    def test_long_label_is_squeezed_into_a_narrow_picture(self):
        r = render_svg(b"img", "image/jpeg", 480, 640, parse_annotations([{**BOX, "label": "x" * 60}]))
        self.assertIn('<rect x="0.0" y="-41.5" width="480.0"', r.svg)
        self.assertIn('lengthAdjust="spacingAndGlyphs">' + "x" * 60, r.svg)


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
        self.assertEqual((p["capture"], p["source"], p["width"], p["height"], p["caption"]), (2, "002-inspect", 300, 200 + 38, "goes to pin 5"))  # + the label band
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
