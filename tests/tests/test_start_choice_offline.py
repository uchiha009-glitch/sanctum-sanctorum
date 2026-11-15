"""Offline tests for the /start return-visit choice and the 50 MB
retrieval explanation — no real Telegram calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers (decided with Aseem, Nov 15, 2026):
  * a new user (or a user with an empty vault) gets the plain welcome;
  * a returning user WITH files gets a choice on /start — "Keep my
    files" or "Start fresh from #1" — because the bot cannot detect a
    Telegram Clear History, so it asks instead of guessing;
  * "Keep" shows the welcome and deletes nothing;
  * "Start fresh" shows a files-only wipe confirmation (count + size,
    notes/links/reminders stay), and confirming wipes ONLY the
    tapping user's files — other users untouched — and the next file
    stored is #1 again;
  * cancelling keeps everything;
  * the 50 MB rule is explained where it matters: the welcome, /help,
    the save reply for a big file, and the /get failure for a big file
    whose original message is gone.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Make the project root importable (bot.py, vault.py, config.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as bot_module
import config
from vault import Store

OWNER = 111
FRIEND = 222
BIG = 60 * 1024 * 1024  # 60 MB — over the 50 MB send limit


class FakeMessage:
    def __init__(self, document=None, caption="", chat_id=111,
                 message_id=1):
        self.text = ""
        self.caption = caption
        self.document = document
        self.photo = None
        self.video = None
        self.audio = None
        self.voice = None
        self.chat_id = chat_id
        self.message_id = message_id
        self.replies = []  # list of (text, kwargs)

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeCallbackQuery:
    def __init__(self, data, user_id=OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answered = False
        self.edits = []  # list of (text, kwargs)

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeUpdate:
    def __init__(self, user_id=OWNER, callback_query=None, message=None):
        self.message = message or FakeMessage()
        self.effective_message = self.message
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=user_id)
        self.callback_query = callback_query


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=None)
        self.bot = bot


def _tg_file(file_id, file_name=None, file_size=0):
    return SimpleNamespace(file_id=file_id, file_name=file_name,
                           file_size=file_size)


def _all_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


class StartChoiceTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.old_token, self.old_owner = config.BOT_TOKEN, config.OWNER_ID
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "x"
        config.OWNER_ID = OWNER
        bot_module.STORE = self.store

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID = self.old_token, self.old_owner
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def _seed(self):
        self.store.add_file(file_id="o1", filename="one.txt",
                            file_type="document", data=b"1", user_id=OWNER)
        self.store.add_file(file_id="o2", filename="two.txt",
                            file_type="document", data=b"2", user_id=OWNER)
        self.store.add_file(file_id="f1", filename="friend.txt",
                            file_type="document", data=b"f", user_id=FRIEND)
        self.store.add_note("owner note", user_id=OWNER)
        self.store.add_link("https://example.com", "link", user_id=OWNER)

    async def _run_start(self, user_id=OWNER):
        update = FakeUpdate(user_id=user_id)
        await bot_module.start(update, FakeContext())
        return update

    async def test_new_user_start_shows_welcome_no_buttons(self):
        update = await self._run_start()
        self.assertEqual(len(update.message.replies), 1)
        text, kwargs = update.message.replies[0]
        self.assertIn("Welcome to your Sanctum Sanctorum", text)
        self.assertIsNone(kwargs.get("reply_markup"))

    async def test_returning_user_start_asks_keep_or_fresh(self):
        self._seed()
        update = await self._run_start()
        text, kwargs = update.message.replies[0]
        self.assertIn("still has 2 file(s)", text)
        self.assertIn("start fresh from #1?", text)
        buttons = _all_buttons(kwargs["reply_markup"])
        self.assertEqual([b.callback_data for b in buttons],
                         ["start:keep", "start:fresh"])

    async def test_start_keep_shows_welcome_and_deletes_nothing(self):
        self._seed()
        cb = FakeCallbackQuery("start:keep", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=cb)
        await bot_module.start_choice(update, FakeContext())
        self.assertIn("Welcome to your Sanctum Sanctorum", cb.edits[0][0])
        self.assertEqual(self.store.stats(user_id=OWNER)["total_files"], 2)

    async def test_start_fresh_shows_files_only_confirmation(self):
        self._seed()
        cb = FakeCallbackQuery("start:fresh", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=cb)
        await bot_module.start_choice(update, FakeContext())
        text, kwargs = cb.edits[0]
        self.assertIn("permanently delete 2 file(s)", text)
        self.assertIn("notes, links and reminders stay", text)
        buttons = _all_buttons(kwargs["reply_markup"])
        self.assertEqual([b.callback_data for b in buttons],
                         ["fresh:yes", "fresh:no"])

    async def test_fresh_yes_wipes_own_files_only_and_restarts_at_1(self):
        self._seed()
        cb = FakeCallbackQuery("fresh:yes", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=cb)
        await bot_module.fresh_confirm(update, FakeContext())
        self.assertIn("Vault cleared: 2 file(s) deleted", cb.edits[0][0])
        self.assertIn("next file will be #1", cb.edits[0][0])
        self.assertEqual(self.store.stats(user_id=OWNER)["total_files"], 0)
        # Notes, links and the friend's files are untouched.
        _, n_notes = self.store.list_notes(limit=10, user_id=OWNER)
        self.assertEqual(n_notes, 1)
        _, n_links = self.store.list_links(limit=10, user_id=OWNER)
        self.assertEqual(n_links, 1)
        self.assertEqual(self.store.stats(user_id=FRIEND)["total_files"], 1)
        # The next file stored is #1 again.
        rec = self.store.add_file(
            file_id="n1", filename="new.txt", file_type="document",
            data=b"n", user_id=OWNER)
        self.assertEqual(rec.user_num, 1)

    async def test_fresh_no_keeps_everything(self):
        self._seed()
        cb = FakeCallbackQuery("fresh:no", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=cb)
        await bot_module.fresh_confirm(update, FakeContext())
        self.assertIn("untouched", cb.edits[0][0])
        self.assertEqual(self.store.stats(user_id=OWNER)["total_files"], 2)

    async def test_fresh_yes_on_empty_vault_is_harmless(self):
        cb = FakeCallbackQuery("fresh:yes", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=cb)
        await bot_module.fresh_confirm(update, FakeContext())
        self.assertIn("Vault cleared: 0 file(s) deleted", cb.edits[0][0])

    async def test_big_file_save_warns_about_clearing(self):
        doc = _tg_file("big1", "big.zip", BIG)
        message = FakeMessage(document=doc, chat_id=OWNER, message_id=7)
        update = FakeUpdate(user_id=OWNER, message=message)
        await bot_module.save_file(update, FakeContext())
        reply = message.replies[0][0]
        self.assertIn("Stored #1 big.zip", reply)
        self.assertIn("cleared this chat", reply)
        self.assertIn("won't be able to get it back", reply)

    async def test_small_file_save_has_no_clearing_warning(self):
        doc = _tg_file("s1", "small.txt", 1000)
        message = FakeMessage(document=doc, chat_id=OWNER, message_id=8)
        update = FakeUpdate(user_id=OWNER, message=message)
        await bot_module.save_file(update, FakeContext())
        self.assertNotIn("cleared this chat", message.replies[0][0])

    def test_welcome_and_help_explain_the_50mb_rule(self):
        self.assertIn("50 MB", bot_module.WELCOME_TEXT)
        self.assertIn("clear this chat", bot_module.WELCOME_TEXT)
        self.assertIn("can't be retrieved", bot_module.HELP_TEXT)

    def test_large_file_details_names_clearing(self):
        self._seed()
        rec = self.store.get_file(1, user_id=OWNER)
        text = bot_module._large_file_details(rec)
        self.assertIn("cleared this chat", text)
        self.assertIn("can't retrieve this file anymore", text)


if __name__ == "__main__":
    unittest.main()
