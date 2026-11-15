"""Offline tests for the short Menu — no real Telegram calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers:
  * the default Menu is exactly the 9 essentials, in order;
  * the owner's menu is those 9 plus /chat, /admin, /users, /ban, /unban;
  * ALL 27 implemented commands are still registered (typed-only ones
    included) — only the Menu list is shorter, never the command set;
  * hidden commands still work when typed (/list, /images, /delnote,
    /setkey, /mykey, /delkey, /ask);
  * /files and /list show filter buttons and tapping one switches view;
  * /help shows Essentials + More, and hides the admin section from
    non-owners.
"""

from __future__ import annotations

import os
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
OTHER = 222

ESSENTIALS = [
    "start", "help", "files", "search", "get", "stats", "note",
    "links", "analyse",
]

# /chat is the keeper's alone (Nov 15, 2026): on the owner menu only,
# never on the default menu — but still a registered command.
KEEPER_ONLY = ["chat"]

TYPED_ONLY = [
    "list", "images", "videos", "audio", "docs", "apps", "delete",
    "clear", "notes", "delnote", "dellink", "remind", "ask",
    "setkey", "delkey", "mykey",
]

ALL_COMMANDS = ESSENTIALS + KEEPER_ONLY + TYPED_ONLY


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []  # list of (text, reply_markup)

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))


class FakeCallbackQuery:
    def __init__(self, data, user_id=OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answered = False
        self.edits = []  # list of (text, reply_markup)

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeUpdate:
    def __init__(self, text="", user_id=OWNER, callback_query=None):
        self.message = FakeMessage(text)
        self.effective_message = self.message
        self.effective_user = FakeUser(user_id)
        self.effective_chat = SimpleNamespace(id=user_id)
        self.callback_query = callback_query


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=None)
        self.bot = None


def _seed_vault(store):
    store.add_file(file_id="p1", filename="pic.jpg", file_type="photo",
                   data=b"p", user_id=OWNER)
    store.add_file(file_id="v1", filename="clip.mp4", file_type="video",
                   data=b"v", user_id=OWNER)
    store.add_file(file_id="a1", filename="song.mp3", file_type="audio",
                   data=b"a", user_id=OWNER)
    store.add_file(file_id="d1", filename="notes.pdf", file_type="document",
                   data=b"d", user_id=OWNER)
    store.add_file(file_id="d2", filename="app.apk", file_type="document",
                   data=b"k", user_id=OWNER)


def _all_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


class ShortMenuTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.old_token, self.old_owner = config.BOT_TOKEN, config.OWNER_ID
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = OWNER
        bot_module.STORE = self.store
        _seed_vault(self.store)

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID = self.old_token, self.old_owner
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    async def _run(self, handler, user_id=OWNER, **kwargs):
        update = FakeUpdate(user_id=user_id, **kwargs)
        await handler(update, FakeContext(**kwargs))
        return update

    def test_default_menu_is_exactly_the_nine_essentials(self):
        self.assertEqual([cmd for cmd, _ in bot_module.BOT_COMMANDS],
                         ESSENTIALS)

    def test_owner_menu_is_nine_plus_chat_plus_admin(self):
        for admin_cmd in ("admin", "users", "ban", "unban"):
            self.assertNotIn(admin_cmd,
                             [c for c, _ in bot_module.BOT_COMMANDS])
        self.assertEqual(
            [c for c, _ in bot_module.ADMIN_COMMANDS],
            ["admin", "users", "ban", "unban"],
        )
        # /chat is the keeper's alone: on the owner menu, never on
        # the default one.
        self.assertNotIn("chat", [c for c, _ in bot_module.BOT_COMMANDS])
        self.assertEqual(
            [c for c, _ in bot_module.KEEPER_COMMANDS],
            ["chat"],
        )

    async def test_set_bot_commands_publishes_short_menu(self):
        from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeChat

        class FakeBot:
            def __init__(self):
                self.calls = []

            async def set_my_commands(self, commands, scope=None):
                self.calls.append((commands, scope))

        fake_app = SimpleNamespace(bot=FakeBot())
        await bot_module.set_bot_commands(fake_app)
        self.assertEqual(len(fake_app.bot.calls), 2)

        default_commands, default_scope = fake_app.bot.calls[0]
        self.assertIsInstance(default_scope, BotCommandScopeAllPrivateChats)
        self.assertEqual([c.command for c in default_commands], ESSENTIALS)

        owner_commands, owner_scope = fake_app.bot.calls[1]
        self.assertIsInstance(owner_scope, BotCommandScopeChat)
        self.assertEqual(owner_scope.chat_id, OWNER)
        self.assertEqual([c.command for c in owner_commands],
                         ESSENTIALS + ["chat", "admin", "users", "ban",
                                       "unban"])

    def test_all_27_commands_still_registered(self):
        # build_app() constructs the python-telegram-bot HTTP client,
        # which chokes on this VM's bracketed-IPv6 NO_PROXY entries, so
        # the proxy variables are cleared for the duration of the call
        # (no network is used — the token is a dummy).
        proxy_vars = ("http_proxy", "https_proxy", "HTTP_PROXY",
                      "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
                      "NO_PROXY", "no_proxy")
        saved_env = {k: os.environ.pop(k) for k in proxy_vars
                     if k in os.environ}
        old_data_dir, old_db_path = config.DATA_DIR, config.DB_PATH
        old_store = bot_module.STORE
        try:
            config.DATA_DIR = Path(self.tmp.name) / "app-data"
            config.DB_PATH = config.DATA_DIR / "vault.db"
            app = bot_module.build_app()
            registered = set()
            for handlers in app.handlers.values():
                for h in handlers:
                    registered |= set(getattr(h, "commands", set()) or set())
            for cmd in ALL_COMMANDS:
                self.assertIn(cmd, registered, cmd)
            for admin_cmd in ("admin", "users", "ban", "unban"):
                self.assertIn(admin_cmd, registered, admin_cmd)
        finally:
            bot_module.STORE.close()
            bot_module.STORE = old_store
            config.DATA_DIR, config.DB_PATH = old_data_dir, old_db_path
            os.environ.update(saved_env)

    async def test_list_still_works_and_shows_filter_buttons(self):
        update = await self._run(bot_module.list_cmd)
        text, markup = update.message.replies[0]
        self.assertIn("pic.jpg", text)
        self.assertIn("clip.mp4", text)
        buttons = _all_buttons(markup)
        by_data = {b.callback_data: b.text for b in buttons}
        for key in ("files", "images", "videos", "audio", "docs", "apps"):
            self.assertIn(f"cat:{key}:0", by_data, key)
        self.assertTrue(by_data["cat:files:0"].startswith("✓ "))

    async def test_files_view_shows_filter_buttons_with_current_marked(self):
        update = await self._run(bot_module.images_cmd)
        text, markup = update.message.replies[0]
        self.assertIn("pic.jpg", text)
        self.assertNotIn("clip.mp4", text)
        by_data = {b.callback_data: b.text for b in _all_buttons(markup)}
        self.assertTrue(by_data["cat:images:0"].startswith("✓ "))
        self.assertFalse(by_data["cat:files:0"].startswith("✓ "))

    async def test_filter_button_switches_to_images(self):
        cb = FakeCallbackQuery("cat:images:0", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=cb)
        await bot_module.category_page(update, FakeContext())
        self.assertTrue(cb.answered)
        text, _ = cb.edits[0]
        self.assertIn("Your images", text)
        self.assertIn("pic.jpg", text)
        self.assertNotIn("clip.mp4", text)

    async def test_filter_button_switches_to_apps(self):
        cb = FakeCallbackQuery("cat:apps:0", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=cb)
        await bot_module.category_page(update, FakeContext())
        self.assertTrue(cb.answered)
        text, _ = cb.edits[0]
        self.assertIn("Your APKs & archives", text)
        self.assertIn("app.apk", text)
        self.assertNotIn("notes.pdf", text)

    async def test_images_command_still_works_when_typed(self):
        update = await self._run(bot_module.images_cmd)
        text, _ = update.message.replies[0]
        self.assertIn("pic.jpg", text)
        self.assertNotIn("clip.mp4", text)
        self.assertNotIn("song.mp3", text)

    async def test_delnote_still_works_when_typed(self):
        note = self.store.add_note("to be deleted", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER)
        await bot_module.delnote_cmd(update, FakeContext(args=[str(note.id)]))
        text, _ = update.message.replies[0]
        self.assertIn(f"Deleted note #{note.id}", text)
        self.assertIsNone(self.store.get_note(note.id, user_id=OWNER))

    async def test_key_commands_still_work_when_typed(self):
        update = FakeUpdate(user_id=OWNER)
        await bot_module.mykey_cmd(update, FakeContext())
        self.assertIn("not linked", update.message.replies[0][0])

        key = "AIza" + "x" * 36
        update = FakeUpdate(user_id=OWNER)
        await bot_module.setkey_cmd(update, FakeContext(args=[key]))
        self.assertIn("linked", update.message.replies[0][0])

        update = FakeUpdate(user_id=OWNER)
        await bot_module.mykey_cmd(update, FakeContext())
        self.assertIn("linked", update.message.replies[0][0])
        self.assertNotIn(key, update.message.replies[0][0])

        update = FakeUpdate(user_id=OWNER)
        await bot_module.delkey_cmd(update, FakeContext())
        self.assertIn("removed", update.message.replies[0][0])

        update = FakeUpdate(user_id=OWNER)
        await bot_module.mykey_cmd(update, FakeContext())
        self.assertIn("not linked", update.message.replies[0][0])

    async def test_ask_still_works_when_typed(self):
        update = FakeUpdate(user_id=OWNER)
        await bot_module.ask_cmd(update, FakeContext(args=["2", "+", "2"]))
        self.assertIn("4", update.message.replies[0][0])

    async def test_help_shows_essentials_and_more(self):
        update = FakeUpdate(user_id=OTHER)
        await bot_module.help_cmd(update, FakeContext())
        text = update.message.replies[0][0]
        self.assertIn("*Essentials", text)
        self.assertIn("*More", text)
        for cmd in ESSENTIALS:
            self.assertIn(f"/{cmd}", text, cmd)
        # /chat is the keeper's alone: hidden from non-owners' /help.
        self.assertNotIn("/chat", text)
        for cmd in ("list", "images", "videos", "audio", "docs", "apps",
                    "delete", "clear", "notes", "delnote", "dellink",
                    "remind", "ask", "setkey", "delkey", "mykey"):
            self.assertIn(f"/{cmd}", text, cmd)
        # Admin section stays hidden from non-owners.
        for cmd in ("/admin", "/users", "/ban", "/unban"):
            self.assertNotIn(cmd, text, cmd)

    async def test_help_shows_admin_section_only_to_owner(self):
        update = FakeUpdate(user_id=OWNER)
        await bot_module.help_cmd(update, FakeContext())
        text = update.message.replies[0][0]
        for cmd in ("/chat", "/admin", "/users", "/ban", "/unban"):
            self.assertIn(cmd, text, cmd)


if __name__ == "__main__":
    unittest.main()
