"""The speaking voice: labels for the dashboard's dropdown, choosing one there (a sample is said, and
the choice is kept for the next run in the settings file) and --speech-voice overriding it.

Run with: uv run python -m unittest discover tests
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_eyes import settings
from agent_eyes.agent import CaptureBudget
from agent_eyes.voice import Voice
from agent_eyes.web.live import LiveSession
from agent_eyes.web.voice import LiveVoice, VoiceOptions

AVA = Voice("com.apple.voice.premium.en-US.Ava", "Ava (Premium)", "en-US", "premium")
DANIEL = Voice("com.apple.voice.compact.en-GB.Daniel", "Daniel", "en-GB", "compact")


class FakeHelper:
    def __init__(self):
        self.sent: list[str] = []

    async def set_voice(self, identifier: str) -> None:
        self.sent.append(f"voice {identifier}")

    async def stop_speaking(self) -> None:
        self.sent.append("stop_speaking")

    async def speak(self, text: str) -> None:
        self.sent.append(f"speak {text}")


class VoiceLabelTest(unittest.TestCase):
    def test_label(self):
        self.assertEqual(AVA.short_name, "Ava")
        self.assertEqual(AVA.label("en-US"), "Ava — Premium")
        self.assertEqual(DANIEL.label("en-US"), "Daniel — Compact · en-GB")
        self.assertEqual(DANIEL.label(None), "Daniel — Compact")

    def test_of_event(self):
        v = Voice.of({"identifier": "x", "name": "Bubbles", "language": "en-US", "quality": "compact", "natural": False})
        self.assertFalse(v.natural)


class ChooseVoiceTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"AGENT_EYES_SETTINGS": str(self.dir / "settings.json")})
        env.start()
        self.addCleanup(env.stop)
        self.session = LiveSession(None, CaptureBudget(), self.dir, model="test")
        self.addCleanup(self.session._log.close)

    def voice(self, options: VoiceOptions = VoiceOptions()) -> LiveVoice:
        self.session.voice = voice = LiveVoice(self.session, options)
        voice.locale, voice.voices, voice.voice = "en-US", [AVA, DANIEL], AVA
        return voice

    def test_choose_says_a_sample_and_is_kept(self):
        async def run():
            voice = self.voice()
            voice._helper = helper = FakeHelper()
            voice.choose_voice(DANIEL.identifier)
            await voice._readout_task
            return voice, helper
        voice, helper = asyncio.run(run())
        self.assertEqual(voice.voice, DANIEL)
        self.assertEqual(helper.sent, [f"voice {DANIEL.identifier}", "stop_speaking", "speak This is Daniel."])
        self.assertEqual(json.loads(settings.path().read_text()), {"voice": DANIEL.identifier})
        self.assertEqual(self.session.summary()["voice"]["voice"], DANIEL.identifier)
        self.assertEqual([v["label"] for v in self.session.summary()["voice"]["voices"]],
                         ["Ava — Premium", "Daniel — Compact · en-GB"])
        # The next run starts with it.
        self.assertEqual(LiveVoice(self.session, VoiceOptions())._voice_ref, DANIEL.identifier)

    def test_unknown_voice(self):
        voice = self.voice()
        with self.assertRaises(ValueError):
            voice.choose_voice("com.example.nobody")
        self.assertEqual(voice.voice, AVA)
        self.assertFalse(settings.path().exists())

    def test_command_line_voice_wins_for_this_run(self):
        settings.save(voice=DANIEL.identifier, other=1)
        self.assertEqual(LiveVoice(self.session, VoiceOptions(voice="Ava"))._voice_ref, "Ava")
        self.assertEqual(settings.load(), {"voice": DANIEL.identifier, "other": 1})


if __name__ == "__main__":
    unittest.main()
