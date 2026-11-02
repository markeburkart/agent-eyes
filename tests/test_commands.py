"""Closing a voice conversation (the closing phrases, "ok thanks" etc., and Focus.close()) and the
spoken actions: clearing the agent's context ("new conversation") and dismissing popovers ("dismiss",
"dismiss all").

Run with: uv run python -m unittest discover tests
"""

import unittest
from collections.abc import Callable

from agent_eyes.voice.commands import (
    ACTIONS,
    Action,
    Command,
    ConversationEnded,
    ConversationOpened,
    Focus,
    FollowUpHeard,
    Interrupted,
    WakeHeard,
    action_for,
    is_end,
    voice_commands,
)
from agent_eyes.voice.echo import Echo
from agent_eyes.voice.helper import Transcript


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Done(Exception):
    """The script ran out."""


class FakeSource:
    """Plays `script`: a Transcript is heard, None is silence for the whole timeout, a callable runs
    (e.g. a button press) and then counts as silence."""

    def __init__(self, clock: Clock, script: list[Transcript | None | Callable[[], None]], echo: Echo | None = None):
        self.clock = clock
        self.script = list(script)
        self.echo = echo or Echo(clock=clock)
        self.stopped = 0

    async def next(self, timeout: float | None = None) -> Transcript | None:
        if not self.script:
            raise Done
        item = self.script.pop(0)
        if isinstance(item, Transcript):
            return item
        if callable(item):
            item()
        self.clock.now += timeout or 0
        return None

    async def finalize(self) -> None:
        pass

    async def stop_speaking(self) -> None:
        self.stopped += 1
        if self.echo.speaking:
            self.echo.done()


async def run(source: FakeSource, focus: Focus, actions: dict[str, tuple[str, ...]] | None = None) -> list:
    events = []
    try:
        async for event in voice_commands(source, focus=focus, actions=actions):
            events.append(event)
    except Done:
        pass
    return events


def heard(text: str, final: bool = True) -> Transcript:
    return Transcript(text, final)


class IsEndTest(unittest.TestCase):
    def test_ok_thanks_closes(self):
        for said in ("ok thanks", "okay thanks", "ok thank you", "Okay, thank you.", "OK thanks!"):
            with self.subTest(said=said):
                self.assertTrue(is_end(said))

    def test_ok_alone_does_not_close(self):
        self.assertFalse(is_end("ok"))
        self.assertFalse(is_end("okay"))

    def test_thanks_mid_sentence_does_not_close(self):
        for said in ("thanks, now check pin five", "ok thanks, is the red wire on pin five?",
                     "is the red wire on pin five thanks", "check it again, thanks"):
            with self.subTest(said=said):
                self.assertFalse(is_end(said))


class ActionForTest(unittest.TestCase):
    def test_new_conversation(self):
        for said in ("new conversation", "New conversation.", "start over", "clear context"):
            with self.subTest(said=said):
                self.assertEqual(action_for(said), "new_conversation")

    def test_dismiss_closes_the_top_one(self):
        for said in ("dismiss", "Dismiss.", "dismiss that", "dismiss it", "Dismissed!"):
            with self.subTest(said=said):
                self.assertEqual(action_for(said), "dismiss")

    def test_dismiss_all(self):
        for said in ("dismiss all", "Dismiss all.", "Dismissal.", "dismiss everything", "dismiss them all", "dismiss all of them"):
            with self.subTest(said=said):
                self.assertEqual(action_for(said), "dismiss_all")

    def test_request_mentioning_a_phrase_is_not_an_action(self):
        for said in ("let's start over with pin five", "is this a new conversation", "start over the soldering",
                     "dismiss the red wire", "should I dismiss this warning", "what does dismiss mean", "close it"):
            with self.subTest(said=said):
                self.assertIsNone(action_for(said))

    def test_only_the_actions_given(self):
        self.assertIsNone(action_for("dismiss", {"new_conversation": ACTIONS["new_conversation"]}))

    def test_phrases_are_unique(self):
        phrases = [p for ps in ACTIONS.values() for p in ps]
        self.assertEqual(len(phrases), len(set(phrases)))


class ConversationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.focus = Focus(30, clock=self.clock)
        self.focus.start()  # a conversation is live

    async def test_ok_thanks_closes_live_conversation(self):
        events = await run(FakeSource(self.clock, [heard("ok thanks"), None]), self.focus)
        self.assertEqual(events, [ConversationOpened(), FollowUpHeard(), ConversationEnded("you closed it")])
        self.assertFalse(self.focus.active)

    async def test_request_with_thanks_mid_sentence_is_sent(self):
        events = await run(FakeSource(self.clock, [heard("ok thanks, is the red wire on pin five?"), None]), self.focus)
        self.assertEqual(events, [ConversationOpened(), FollowUpHeard(), Command("ok thanks, is the red wire on pin five")])
        self.assertTrue(self.focus.active)

    def reading_out(self) -> Echo:
        echo = Echo(clock=self.clock)
        echo.begin()
        echo.spoke(["the", "red", "wire", "is", "on", "pin", "five"])
        return echo

    async def test_ok_only_stops_the_readout(self):
        source = FakeSource(self.clock, [heard("the red", False), heard("the red ok", False)] + [None] * 8,
                            self.reading_out())
        events = await run(source, self.focus)
        self.assertEqual(events, [ConversationOpened(), Interrupted("ok")])
        self.assertEqual(source.stopped, 1)
        self.assertTrue(self.focus.active)

    async def test_ok_thanks_during_readout_stops_it_and_closes(self):
        # The recognizer hears "ok" first (which stops the readout), then "ok thanks".
        source = FakeSource(self.clock, [heard("the red", False), heard("the red ok", False),
                                         heard("the red okay thanks", False)] + [None] * 8,
                            self.reading_out())
        events = await run(source, self.focus)
        self.assertEqual(events, [ConversationOpened(), Interrupted("ok"), ConversationEnded("you closed it")])
        self.assertEqual(source.stopped, 1)
        self.assertFalse(self.focus.active)

    async def test_ok_thank_you_heard_at_once_during_readout(self):
        source = FakeSource(self.clock, [heard("the red ok thank you")] + [None] * 8, self.reading_out())
        events = await run(source, self.focus)
        self.assertEqual(events, [ConversationOpened(), ConversationEnded("you closed it")])
        self.assertEqual(source.stopped, 1)
        self.assertFalse(self.focus.active)

    async def test_close_from_outside(self):
        source = FakeSource(self.clock, [lambda: self.focus.close("button")] + [None] * 3)
        events = await run(source, self.focus)
        self.assertEqual(events, [ConversationOpened(), ConversationEnded("button")])
        self.assertFalse(self.focus.active)

    async def test_close_from_outside_drops_dictation(self):
        source = FakeSource(self.clock, [heard("is the red", False), lambda: self.focus.close("button")] + [None] * 3)
        events = await run(source, self.focus)
        self.assertEqual(events, [ConversationOpened(), FollowUpHeard(), ConversationEnded("button")])

    async def test_close_from_outside_stops_readout(self):
        source = FakeSource(self.clock, [lambda: self.focus.close("button")] + [None] * 8, self.reading_out())
        events = await run(source, self.focus)
        self.assertEqual(events, [ConversationOpened(), ConversationEnded("button")])
        self.assertEqual(source.stopped, 1)

    async def test_close_while_waiting_drops_a_request_about_to_be_sent(self):
        source = FakeSource(self.clock, [heard("is the red wire on pin five"),
                                         lambda: self.focus.close("button")] + [None] * 2)
        events = await run(source, self.focus)
        self.assertEqual(events, [ConversationOpened(), FollowUpHeard(), ConversationEnded("button")])

    async def test_new_conversation_after_wake_word(self):
        self.focus.end()
        events = await run(FakeSource(self.clock, [heard("hey eyes, new conversation"), None]), self.focus, ACTIONS)
        self.assertEqual(events, [WakeHeard(), Action("new_conversation"), ConversationOpened()])
        self.assertTrue(self.focus.active)

    async def test_new_conversation_is_a_request_without_actions(self):
        events = await run(FakeSource(self.clock, [heard("new conversation"), None]), self.focus)
        self.assertEqual(events, [ConversationOpened(), FollowUpHeard(), Command("new conversation")])

    async def test_dismiss_after_wake_word(self):
        self.focus.end()
        events = await run(FakeSource(self.clock, [heard("hey eyes, dismiss"), None]), self.focus, ACTIONS)
        self.assertEqual(events, [WakeHeard(), Action("dismiss"), ConversationOpened()])
        self.assertTrue(self.focus.active)

    async def test_wake_word_revised_away(self):
        # The recognizer heard "hey eyes" (dictation started) but its final text says "hey, guys".
        self.focus.end()
        source = FakeSource(self.clock, [heard("hey eyes", False), heard("Hey, guys. Dismiss."), None])
        events = await run(source, self.focus, ACTIONS)
        self.assertEqual(events, [WakeHeard(), Action("dismiss"), ConversationOpened()])

    async def test_wake_word_revised_away_in_a_request(self):
        self.focus.end()
        source = FakeSource(self.clock, [heard("hey eyes is", False), heard("Hey guys, is the red wire on pin five?"), None])
        events = await run(source, self.focus)
        self.assertEqual(events, [WakeHeard(), Command("is the red wire on pin five"), ConversationOpened()])

    async def test_dismiss_all_in_live_conversation(self):
        events = await run(FakeSource(self.clock, [heard("dismiss all"), None]), self.focus, ACTIONS)
        self.assertEqual(events, [ConversationOpened(), FollowUpHeard(), Action("dismiss_all")])
        self.assertTrue(self.focus.active)

    async def test_dismiss_is_a_request_without_actions(self):
        events = await run(FakeSource(self.clock, [heard("dismiss"), None]), self.focus)
        self.assertEqual(events, [ConversationOpened(), FollowUpHeard(), Command("dismiss")])

    async def test_dismiss_in_a_sentence_goes_to_the_agent(self):
        events = await run(FakeSource(self.clock, [heard("dismiss the box on the left"), None]), self.focus, ACTIONS)
        self.assertEqual(events, [ConversationOpened(), FollowUpHeard(), Command("dismiss the box on the left")])


if __name__ == "__main__":
    unittest.main()
