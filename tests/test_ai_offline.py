"""Offline tests for the Sanctum persona + /chat — no real Gemini calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers:
  * the local Sanctum persona (greetings, how-are-you, identity, thanks,
    farewell) — all in character, no AI involved,
  * answer_chat delegating vault/arithmetic questions to the /ask rules,
  * /chat WITHOUT a Gemini key -> in-character local reply (not an error),
  * /chat WITH a mocked Gemini call -> the mocked AI reply (+ history),
  * model fallback (gemini-3.5-flash-lite unavailable -> gemini-3.5-flash),
  * API error handling (rate limit -> honest unavailable message),
  * plain-text messages -> in-character local reply, AI never called,
  * the owner gate on /chat and on plain text.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Make the project root importable (bot.py, vault.py, assistant.py, ai.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai as ai_module
import bot as bot_module
import config
from assistant import answer_chat
from vault import Store


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeUpdate:
    def __init__(self, text="", user_id=111):
        self.message = FakeMessage(text)
        self.effective_message = self.message
        self.effective_user = FakeUser(user_id)
        self.effective_chat = SimpleNamespace(id=user_id)


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=None)
        self.bot = None


class PersonaTestCase(unittest.TestCase):
    """The local assistant answers basic chat in character, no AI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.store.add_file(file_id="1", filename="anatomy.pdf",
                            file_type="document", size=10, data=b"x" * 10, user_id=111)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_greetings(self):
        for greeting in ("hello", "hi", "hey", "hello sanctum", "good morning",
                         "good afternoon", "good evening"):
            out = answer_chat(greeting, self.store, 111)
            self.assertIn("keeper", out, greeting)
            self.assertIn("Sanctum", out, greeting)

    def test_how_are_you(self):
        out = answer_chat("how are you?", self.store, 111)
        self.assertIn("wards hold", out)
        self.assertIn("keeper", out)

    def test_who_are_you(self):
        out = answer_chat("who are you?", self.store, 111)
        self.assertIn("Sanctum Sanctorum", out)
        self.assertIn("keeper", out)

    def test_what_can_you_do(self):
        out = answer_chat("what can you do?", self.store, 111)
        self.assertIn("no external AI", out)
        self.assertIn("/list", out)

    def test_thanks_and_farewell(self):
        self.assertIn("most welcome", answer_chat("thank you", self.store, 111))
        self.assertIn("Farewell", answer_chat("bye", self.store, 111))
        self.assertIn("keep watch", answer_chat("good night", self.store, 111))

    def test_delegates_to_ask_rules(self):
        out = answer_chat("how many files do I have?", self.store, 111)
        self.assertIn("1 file(s)", out)
        self.assertEqual(answer_chat("2+2", self.store, 111), "2+2 = 4")
        self.assertIn("It is", answer_chat("what time is it?", self.store, 111))

    def test_pointer_when_not_understood(self):
        # A non-owner is never pointed at /chat (keeper-only): the
        # pointer sends them to /help instead.
        out = answer_chat("blorp zorp quux", self.store, 111)
        self.assertNotIn("/chat", out)
        self.assertIn("/help", out)
        self.assertIn("keeper", out)
        owner_out = answer_chat("blorp zorp quux", self.store, 111,
                                is_owner=True)
        self.assertIn("/chat", owner_out)
        self.assertIn("/help", owner_out)


class ChatHandlerTestCase(unittest.IsolatedAsyncioTestCase):
    """Handlers: /chat (with and without a key), plain text, owner gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.old_token, self.old_owner, self.old_key = (
            config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY,
        )
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = 111
        bot_module.STORE = self.store
        self._orig_generate = ai_module._generate_rest
        ai_module._HISTORY.clear()

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY = (
            self.old_token, self.old_owner, self.old_key,
        )
        bot_module.STORE = self.old_store
        ai_module._generate_rest = self._orig_generate
        ai_module._HISTORY.clear()
        self.store.close()
        self.tmp.cleanup()

    def _forbid_ai(self):
        """Replace the Gemini call with one that fails the test if used."""
        async def _boom(*args, **kwargs):
            raise AssertionError("Gemini must not be called here")
        ai_module._generate_rest = _boom

    async def test_chat_without_key_is_in_character(self):
        config.GEMINI_API_KEY = None
        self._forbid_ai()
        update = FakeUpdate(user_id=111)
        context = FakeContext(args=["hello"])
        await bot_module.chat_cmd(update, context)
        self.assertEqual(len(update.message.replies), 1)
        reply = update.message.replies[0]
        self.assertIn("Greetings, keeper", reply)  # real local reply, not an error
        self.assertIn("fuller mind", reply)  # brief in-character aside

    async def test_chat_without_key_answers_a_question(self):
        config.GEMINI_API_KEY = None
        self._forbid_ai()
        update = FakeUpdate(user_id=111)
        context = FakeContext(args=["how", "many", "files", "do", "I", "have?"])
        await bot_module.chat_cmd(update, context)
        self.assertIn("0 file(s)", update.message.replies[0])

    async def test_chat_no_args_usage(self):
        config.GEMINI_API_KEY = None
        update = FakeUpdate(user_id=111)
        await bot_module.chat_cmd(update, FakeContext(args=[]))
        self.assertIn("Speak, keeper", update.message.replies[0])

    async def test_chat_with_mocked_gemini(self):
        config.GEMINI_API_KEY = "fake-key-for-test"
        calls = []

        async def _fake(api_key, model_name, history, message):
            calls.append((api_key, model_name, message))
            return "Mocked AI reply, keeper."

        ai_module._generate_rest = _fake
        update = FakeUpdate(user_id=111)
        await bot_module.chat_cmd(update, FakeContext(args=["tell", "me", "a", "joke"]))
        self.assertEqual(update.message.replies, ["Mocked AI reply, keeper."])
        self.assertEqual(calls[0][1], "gemini-3.5-flash-lite")
        self.assertEqual(calls[0][0], "fake-key-for-test")
        # History recorded: the user message + the model reply.
        history = ai_module._HISTORY[111]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "model")
        self.assertEqual(history[1]["parts"], ["Mocked AI reply, keeper."])

    async def test_chat_model_fallback(self):
        config.GEMINI_API_KEY = "fake-key-for-test"

        async def _fake(api_key, model_name, history, message):
            if model_name == "gemini-3.5-flash-lite":
                raise Exception("404 Not Found: model gemini-3.5-flash-lite")
            return "Fallback model reply."

        ai_module._generate_rest = _fake
        update = FakeUpdate(user_id=111)
        await bot_module.chat_cmd(update, FakeContext(args=["hi"]))
        self.assertEqual(update.message.replies, ["Fallback model reply."])

    async def test_chat_rate_limit_is_honest(self):
        config.GEMINI_API_KEY = "fake-key-for-test"

        async def _boom(*a, **k):
            raise Exception("429 Resource exhausted: quota")

        ai_module._generate_rest = _boom
        update = FakeUpdate(user_id=111)
        await bot_module.chat_cmd(update, FakeContext(args=["hi"]))
        self.assertIn("overworked", update.message.replies[0])
        self.assertIn("/ask", update.message.replies[0])

    async def test_plain_text_is_local_even_with_key(self):
        # Even with a key configured, plain text must NEVER call the AI.
        config.GEMINI_API_KEY = "fake-key-for-test"
        self._forbid_ai()
        update = FakeUpdate(text="hello", user_id=111)
        await bot_module.plain_text(update, FakeContext())
        self.assertEqual(len(update.message.replies), 1)
        self.assertIn("Greetings, keeper", update.message.replies[0])

    async def test_plain_text_pointer_when_not_understood(self):
        config.GEMINI_API_KEY = None
        self._forbid_ai()
        update = FakeUpdate(text="blorp zorp", user_id=111)
        await bot_module.plain_text(update, FakeContext())
        self.assertIn("/chat", update.message.replies[0])
        self.assertIn("/help", update.message.replies[0])

    async def test_non_owner_chat_refused_owner_only(self):
        # /chat is the keeper's alone (Nov 15, 2026): a non-owner gets
        # a short refusal — no AI call, no key check, no BYOK prompt —
        # even when the owner HAS a key in .env.
        config.GEMINI_API_KEY = "fake-key-for-test"
        self._forbid_ai()
        update = FakeUpdate(user_id=222)
        await bot_module.chat_cmd(update, FakeContext(args=["hello"]))
        reply = update.message.replies[0]
        self.assertIn("keeper's counsel", reply)
        self.assertNotIn("/setkey", reply)
        self.assertNotIn("private vault", reply)
        # Plain text is local for everyone now.
        update2 = FakeUpdate(text="hello", user_id=222)
        await bot_module.plain_text(update2, FakeContext())
        self.assertIn("Greetings, keeper", update2.message.replies[0])

    async def test_non_owner_chat_with_linked_key_still_refused(self):
        # Even a linked key does not open /chat to a non-owner.
        self.store.set_user_key(222, "user-own-key-1234567890")
        config.GEMINI_API_KEY = "fake-key-for-test"
        self._forbid_ai()
        update = FakeUpdate(user_id=222)
        await bot_module.chat_cmd(update, FakeContext(args=["hello"]))
        reply = update.message.replies[0]
        self.assertIn("keeper's counsel", reply)
        self.assertNotIn("/setkey", reply)


class AiModuleTestCase(unittest.IsolatedAsyncioTestCase):
    """ai.py itself: availability flag, history trimming, no-key safety."""

    def setUp(self):
        self.old_key = config.GEMINI_API_KEY
        self._orig_generate = ai_module._generate_rest
        ai_module._HISTORY.clear()

    def tearDown(self):
        config.GEMINI_API_KEY = self.old_key
        ai_module._generate_rest = self._orig_generate
        ai_module._HISTORY.clear()

    def test_is_ai_available(self):
        # Availability is per explicit key only — there is NO silent
        # fallback to the owner's configured key.
        config.GEMINI_API_KEY = "owner-key"
        self.assertFalse(ai_module.is_ai_available())
        self.assertFalse(ai_module.is_ai_available(None))
        self.assertFalse(ai_module.is_ai_available(""))
        self.assertTrue(ai_module.is_ai_available("a-user-key"))
        config.GEMINI_API_KEY = None
        self.assertFalse(ai_module.is_ai_available())
        self.assertTrue(ai_module.is_ai_available("a-user-key"))

    async def test_history_trimmed_to_last_10(self):
        config.GEMINI_API_KEY = "fake-key-for-test"

        async def _fake(*a, **k):
            return "r"

        ai_module._generate_rest = _fake
        for i in range(6):
            # The key must be passed explicitly — chat_with_ai never
            # falls back to the owner's configured key on its own.
            await ai_module.chat_with_ai(111, f"message {i}",
                                         api_key="fake-key-for-test")
        history = ai_module._HISTORY[111]
        self.assertEqual(len(history), 10)  # 6 exchanges = 12, trimmed to 10
        # Oldest kept entry is from the 2nd exchange.
        self.assertEqual(history[0]["parts"], ["message 1"])

    async def test_chat_with_ai_without_key_never_calls_api(self):
        config.GEMINI_API_KEY = None

        async def _boom(*a, **k):
            raise AssertionError("must not be called")

        ai_module._generate_rest = _boom
        out = await ai_module.chat_with_ai(111, "hello")
        self.assertIn("no Gemini key", out)


if __name__ == "__main__":
    unittest.main()
