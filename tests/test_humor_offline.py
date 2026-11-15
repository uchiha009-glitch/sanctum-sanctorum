"""Offline tests for the keeper's local humour — no AI involved.

Run from the project root:
    python -m unittest discover -s tests -v

Covers (the "humor without ai missing" bug):
  * "tell me a joke" (and friends) gets a joke from the keeper's own
    local book — and repeat requests rotate to a fresh one,
  * laughing ("haha", "lol", "that was funny") gets a playful reply,
  * teasing ("stupid bot") gets a playful defence, not silence,
  * "i'm bored" gets an offer of something to do,
  * the unmatched fallback (CHAT_POINTER) and the /ask fallback stay
    honest about having no external AI while pointing at /chat, /help,
    and the joke intent — and still contain what the older tests
    require ("/chat", "/help", "keeper", "no external AI", "/list"),
  * the older chitchat intents (greetings, how-are-you, thanks,
    farewells) still work exactly as before.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Make the project root importable (assistant.py, vault.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import assistant
from assistant import _JOKES, answer_ask, answer_chat
from vault import Store


class HumorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        # Reset the joke rotation so each test starts from joke #1.
        assistant._JOKE_INDEX = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_joke_intent_returns_a_local_joke(self):
        for phrase in ("tell me a joke", "joke", "make me laugh",
                       "tell a joke", "something funny"):
            assistant._JOKE_INDEX = 0
            out = answer_chat(phrase, self.store, 111)
            self.assertIn(out, _JOKES, phrase)

    def test_jokes_rotate(self):
        first = answer_chat("tell me a joke", self.store, 111)
        second = answer_chat("tell me a joke", self.store, 111)
        third = answer_chat("tell me a joke", self.store, 111)
        self.assertEqual(first, _JOKES[0])
        self.assertEqual(second, _JOKES[1])
        self.assertEqual(third, _JOKES[2])
        self.assertNotEqual(first, second)

    def test_joke_book_is_not_empty(self):
        self.assertGreaterEqual(len(_JOKES), 3)
        for joke in _JOKES:
            self.assertTrue(joke.strip())

    def test_laughing_gets_a_playful_reply(self):
        for phrase in ("haha", "lol", "that was funny", "lmao",
                       "you're funny"):
            out = answer_chat(phrase, self.store, 111)
            self.assertIn("keeper", out.lower(), phrase)
            self.assertNotIn("slips past my wards", out, phrase)

    def test_teasing_gets_a_playful_defence(self):
        for phrase in ("stupid bot", "dumb bot", "you're useless"):
            out = answer_chat(phrase, self.store, 111)
            self.assertIn("vault", out.lower(), phrase)
            self.assertIn("joke", out.lower(), phrase)

    def test_bored_gets_an_offer(self):
        out = answer_chat("i'm bored", self.store, 111)
        self.assertIn("joke", out.lower())
        self.assertIn("keeper", out.lower())

    def test_fallback_pointer_is_playful_but_points_onwards(self):
        out = answer_chat("blorp zorp quux", self.store, 111)
        self.assertNotIn("/chat", out)
        self.assertIn("/help", out)
        self.assertIn("keeper", out)
        self.assertIn("joke", out.lower())
        self.assertIn("crystal ball", out)
        owner_out = answer_chat("blorp zorp quux", self.store, 111,
                                is_owner=True)
        self.assertIn("/chat", owner_out)

    def test_ask_fallback_is_playful_but_honest(self):
        out = answer_ask("tell me about quantum physics", self.store, 111)
        self.assertIn("no external AI", out)
        self.assertIn("/list", out)
        self.assertIn("joke", out.lower())

    def test_older_chitchat_still_works(self):
        self.assertIn("keeper", answer_chat("hello", self.store, 111))
        self.assertIn("wards hold",
                      answer_chat("how are you?", self.store, 111))
        self.assertIn("most welcome",
                      answer_chat("thank you", self.store, 111))
        self.assertIn("Farewell", answer_chat("bye", self.store, 111))
        self.assertIn("keep watch",
                      answer_chat("good night", self.store, 111))
        self.assertIn("Sanctum Sanctorum",
                      answer_chat("who are you?", self.store, 111))


if __name__ == "__main__":
    unittest.main()
