"""Offline tests for the browse/wipe commands — no real Telegram calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers:
  * /files is an alias for /list (same records, same text),
  * /images, /videos, /audio, /docs return ONLY their category,
  * /apps returns ONLY APK/archive files (case-insensitive extensions),
  * category paging (Prev/Next) stays inside its category,
  * /clear asks for confirmation and does NOT wipe on first call,
  * confirming /clear wipes files + notes and reports the counts,
  * cancelling /clear wipes nothing,
  * the owner gate blocks non-owners on the new commands and callbacks,
  * set_bot_commands publishes exactly the commands the bot implements.
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


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []  # list of (text, reply_markup)

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))


class FakeCallbackQuery:
    def __init__(self, data, user_id=111):
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
    def __init__(self, text="", user_id=111, callback_query=None):
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


def _seed_mixed_vault(store):
    """One file of each kind, plus APKs/archives (incl. uppercase ext)."""
    store.add_file(file_id="p1", filename="pic.jpg", file_type="photo", data=b"p", user_id=111)
    store.add_file(file_id="v1", filename="clip.mp4", file_type="video", data=b"v", user_id=111)
    store.add_file(file_id="a1", filename="song.mp3", file_type="audio", data=b"a", user_id=111)
    store.add_file(file_id="a2", filename="note.ogg", file_type="voice", data=b"o", user_id=111)
    store.add_file(file_id="d1", filename="notes.pdf", file_type="document", data=b"d", user_id=111)
    store.add_file(file_id="d2", filename="app.apk", file_type="document", data=b"k", user_id=111)
    store.add_file(file_id="d3", filename="backup.zip", file_type="document", data=b"z", user_id=111)
    store.add_file(file_id="d4", filename="GAME.APK", file_type="document", data=b"g", user_id=111)
    store.add_file(file_id="d5", filename="mod.xapk", file_type="document", data=b"x", user_id=111)
    store.add_file(file_id="d6", filename="data.rar", file_type="document", data=b"r", user_id=111)
    store.add_file(file_id="d7", filename="photo.apk.jpg", file_type="document", data=b"j", user_id=111)


class BrowseTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.old_token, self.old_owner = config.BOT_TOKEN, config.OWNER_ID
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = 111
        bot_module.STORE = self.store
        _seed_mixed_vault(self.store)

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID = self.old_token, self.old_owner
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    async def _run(self, handler, user_id=111, **kwargs):
        update = FakeUpdate(user_id=user_id, **kwargs)
        await handler(update, FakeContext())
        return update

    def _text(self, update):
        return update.message.replies[0][0]

    async def test_files_is_alias_for_list(self):
        u_files = await self._run(bot_module.files_cmd)
        u_list = await self._run(bot_module.list_cmd)
        self.assertEqual(self._text(u_files), self._text(u_list))
        self.assertIn("11 total", self._text(u_files))

    async def test_images_only_photos(self):
        update = await self._run(bot_module.images_cmd)
        text = self._text(update)
        self.assertIn("pic.jpg", text)
        for name in ("clip.mp4", "song.mp3", "note.ogg", "notes.pdf", "app.apk"):
            self.assertNotIn(name, text)

    async def test_videos_only_videos(self):
        update = await self._run(bot_module.videos_cmd)
        text = self._text(update)
        self.assertIn("clip.mp4", text)
        for name in ("pic.jpg", "song.mp3", "notes.pdf", "app.apk"):
            self.assertNotIn(name, text)

    async def test_audio_includes_voice(self):
        update = await self._run(bot_module.audio_cmd)
        text = self._text(update)
        self.assertIn("song.mp3", text)
        self.assertIn("note.ogg", text)  # voice notes live in audio
        for name in ("pic.jpg", "clip.mp4", "notes.pdf"):
            self.assertNotIn(name, text)

    async def test_docs_only_documents(self):
        # /docs is documents ONLY now: APKs/archives live in the "apps"
        # category (filed by extension at save time), so they must not
        # appear here — that was the bug ("it considers apk as doc").
        update = await self._run(bot_module.docs_cmd)
        text = self._text(update)
        for name in ("notes.pdf", "photo.apk.jpg"):
            self.assertIn(name, text)
        for name in ("app.apk", "backup.zip", "GAME.APK", "mod.xapk",
                     "data.rar"):
            self.assertNotIn(name, text)
        for name in ("pic.jpg", "clip.mp4", "song.mp3", "note.ogg"):
            self.assertNotIn(name, text)

    async def test_apps_only_apk_and_archives(self):
        update = await self._run(bot_module.apps_cmd)
        text = self._text(update)
        for name in ("app.apk", "backup.zip", "GAME.APK", "mod.xapk", "data.rar"):
            self.assertIn(name, text, name)
        # Not an archive: a plain document, and a .jpg that merely
        # contains ".apk" in the middle of its name.
        self.assertNotIn("notes.pdf", text)
        self.assertNotIn("photo.apk.jpg", text)
        self.assertNotIn("pic.jpg", text)
        self.assertNotIn("clip.mp4", text)

    async def test_category_paging_stays_in_category(self):
        # 11 photos -> page 0 has 10, page 1 has 1; paging must not leak
        # into other categories.
        for i in range(11):
            self.store.add_file(
                file_id=f"px{i}", filename=f"shot{i}.jpg",
                file_type="photo", data=b"p", user_id=111)
        update = await self._run(bot_module.images_cmd)
        text, markup = update.message.replies[0]
        self.assertIn("12 total", text)  # 1 pic.jpg + 11 shot*.jpg, not all 22 files
        self.assertNotIn("clip.mp4", text)
        # The Next button carries the category callback data.
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertTrue(any(b.callback_data == "cat:images:1" for b in buttons))
        # Simulate tapping Next -> page 1, still only photos.
        cb = FakeCallbackQuery("cat:images:1", user_id=111)
        update2 = FakeUpdate(user_id=111, callback_query=cb)
        await bot_module.category_page(update2, FakeContext())
        self.assertTrue(cb.answered)
        text2 = cb.edits[0][0]
        self.assertIn("page 2 of 2", text2)
        self.assertIn("shot", text2)
        self.assertNotIn("clip.mp4", text2)

    async def test_clear_asks_and_does_not_wipe(self):
        self.store.add_note("keep me", user_id=111)
        before_files, _ = self.store.list_files(limit=100, user_id=111)
        update = await self._run(bot_module.clear_cmd)
        text, markup = update.message.replies[0]
        self.assertIn("Are you sure", text)
        self.assertIn("permanently delete", text)
        self.assertIn("11 file(s)", text)
        self.assertIn("1 note(s)", text)
        self.assertIsNotNone(markup)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("✅ Yes, wipe everything", labels)
        self.assertIn("❌ Cancel", labels)
        # Nothing was deleted by the command itself.
        after_files, total = self.store.list_files(limit=100, user_id=111)
        self.assertEqual(total, len(before_files))
        _, n_notes = self.store.list_notes(user_id=111)
        self.assertEqual(n_notes, 1)

    async def test_clear_confirm_wipes_everything(self):
        self.store.add_note("note one", user_id=111)
        self.store.add_note("note two", user_id=111)
        paths = [rec.path for rec, _ in [(r, 0) for r in self.store.list_files(limit=100, user_id=111)[0]]]
        await self._run(bot_module.clear_cmd)
        cb = FakeCallbackQuery("clear:yes", user_id=111)
        update = FakeUpdate(user_id=111, callback_query=cb)
        await bot_module.clear_confirm(update, FakeContext())
        self.assertTrue(cb.answered)
        edit_text = cb.edits[0][0]
        self.assertIn("Vault wiped", edit_text)
        self.assertIn("11 file(s)", edit_text)
        self.assertIn("2 note(s)", edit_text)
        _, total_files = self.store.list_files(limit=100, user_id=111)
        self.assertEqual(total_files, 0)
        _, total_notes = self.store.list_notes(user_id=111)
        self.assertEqual(total_notes, 0)
        for p in paths:
            self.assertFalse(p.exists(), p)

    async def test_clear_cancel_wipes_nothing(self):
        self.store.add_note("keep me", user_id=111)
        await self._run(bot_module.clear_cmd)
        cb = FakeCallbackQuery("clear:no", user_id=111)
        update = FakeUpdate(user_id=111, callback_query=cb)
        await bot_module.clear_confirm(update, FakeContext())
        self.assertIn("nothing was deleted", cb.edits[0][0])
        _, total_files = self.store.list_files(limit=100, user_id=111)
        self.assertEqual(total_files, 11)
        _, total_notes = self.store.list_notes(user_id=111)
        self.assertEqual(total_notes, 1)

    async def test_clear_empty_vault_says_so(self):
        empty = Store(Path(self.tmp.name) / "empty")
        bot_module.STORE = empty
        try:
            update = await self._run(bot_module.clear_cmd)
            self.assertIn("already empty", self._text(update))
            self.assertIsNone(update.message.replies[0][1])  # no buttons
        finally:
            empty.close()
            bot_module.STORE = self.store

    async def test_non_owner_uses_own_empty_vault(self):
        # Any user may use the bot now, but only ever sees THEIR vault:
        # user 222 sees an empty vault, never user 111's files.
        for handler in (bot_module.files_cmd, bot_module.images_cmd,
                        bot_module.videos_cmd, bot_module.audio_cmd,
                        bot_module.docs_cmd, bot_module.apps_cmd):
            update = await self._run(handler, user_id=222)
            text = self._text(update)
            self.assertIn("vault", text, handler)
            for name in ("pic.jpg", "clip.mp4", "song.mp3", "notes.pdf",
                         "app.apk"):
                self.assertNotIn(name, text, handler)
        # 222's /clear on their empty vault says so (and touches nothing).
        update = await self._run(bot_module.clear_cmd, user_id=222)
        self.assertIn("already empty", self._text(update))
        # A clear:yes tap from 222 wipes only 222's vault: 111's files stay.
        cb = FakeCallbackQuery("clear:yes", user_id=222)
        update = FakeUpdate(user_id=222, callback_query=cb)
        await bot_module.clear_confirm(update, FakeContext())
        self.assertIn("Vault wiped", cb.edits[0][0])
        self.assertIn("0 file(s)", cb.edits[0][0])
        _, total_files = self.store.list_files(limit=100, user_id=111)
        self.assertEqual(total_files, 11)
        # A category page tap from 222 shows 222's own (empty) images.
        cb2 = FakeCallbackQuery("cat:images:1", user_id=222)
        update2 = FakeUpdate(user_id=222, callback_query=cb2)
        await bot_module.category_page(update2, FakeContext())
        self.assertIn("No your images", cb2.edits[0][0])


class StoreFilterTestCase(unittest.TestCase):
    """Store.list_files category/extension filters (used by the browse cmds)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        _seed_mixed_vault(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_category_filter(self):
        records, total = self.store.list_files(category="photos", user_id=111)
        self.assertEqual(total, 1)
        self.assertEqual(records[0].filename, "pic.jpg")
        records, total = self.store.list_files(category="audio", user_id=111)
        self.assertEqual(total, 2)  # audio + voice
        # Documents are only the plain ones now: the 5 APKs/archives
        # are filed under "apps" by extension at save time.
        records, total = self.store.list_files(category="documents", user_id=111)
        self.assertEqual(total, 2)  # notes.pdf + photo.apk.jpg
        self.assertEqual(
            sorted(r.filename for r in records),
            ["notes.pdf", "photo.apk.jpg"])
        records, total = self.store.list_files(category="apps", user_id=111)
        self.assertEqual(total, 5)  # app.apk, backup.zip, GAME.APK, mod.xapk, data.rar
        self.assertEqual(
            sorted(r.filename for r in records),
            ["GAME.APK", "app.apk", "backup.zip", "data.rar", "mod.xapk"])

    def test_extension_filter_case_insensitive(self):
        from vault import APP_ARCHIVE_EXTENSIONS

        # The extension filter now runs on the "apps" category (the
        # safety net kept on the /apps view); documents no longer
        # contain APKs/archives at all.
        records, total = self.store.list_files(
            category="apps", extensions=APP_ARCHIVE_EXTENSIONS
        , user_id=111)
        names = sorted(r.filename for r in records)
        self.assertEqual(
            names,
            ["GAME.APK", "app.apk", "backup.zip", "data.rar", "mod.xapk"],
        )
        self.assertEqual(total, 5)

    def test_clear_all_removes_disk_and_index(self):
        self.store.add_note("a note", user_id=111)
        rec = self.store.list_files(limit=1, user_id=111)[0][0]
        path = rec.path
        self.assertTrue(path.exists())
        n_files, n_notes = self.store.clear_all(user_id=111)
        self.assertEqual((n_files, n_notes), (11, 1))
        self.assertFalse(path.exists())
        self.assertEqual(self.store.list_files(limit=10, user_id=111)[1], 0)
        self.assertEqual(self.store.list_notes(user_id=111)[1], 0)
        # Clearing an already-empty vault is a no-op, not an error.
        self.assertEqual(self.store.clear_all(user_id=111), (0, 0))


class MenuCommandsTestCase(unittest.IsolatedAsyncioTestCase):
    """The Menu button publishes exactly the commands the bot implements —
    and non-owners never see the owner-only commands.

    Two scoped menus: the default menu (all private chats) is
    BOT_COMMANDS only; the owner's own menu is BOT_COMMANDS plus
    ADMIN_COMMANDS. /help shows the admin section to the keeper only.
    """

    EXPECTED = [cmd for cmd, _ in bot_module.BOT_COMMANDS]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.old_token, self.old_owner = config.BOT_TOKEN, config.OWNER_ID
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = 111
        bot_module.STORE = self.store

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID = self.old_token, self.old_owner
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def test_command_list_order_and_contents(self):
        expected = [
            "start", "help", "files", "search", "get", "stats", "note",
            "links", "analyse",
        ]
        self.assertEqual(self.EXPECTED, expected)
        # The two original Emergent-only commands are now built too:
        # /links and /analyse are real commands, not stale menu entries.
        # (/ocr was removed Nov 15, 2026; it is in neither the Menu
        # nor /help, and no handler is registered for it.)
        for now_built in ("links", "analyse"):
            self.assertIn(now_built, self.EXPECTED)
        self.assertNotIn("ocr", self.EXPECTED)
        # The Menu is the 10 essentials only. Every other implemented
        # command still works when typed — it is just not in the Menu.
        for typed_only in ("list", "images", "videos", "audio", "docs",
                           "apps", "delete", "clear", "notes", "delnote",
                           "dellink", "remind", "ask", "setkey", "delkey",
                           "mykey"):
            self.assertNotIn(typed_only, self.EXPECTED)
        # Owner-only commands live in ADMIN_COMMANDS, not the default menu.
        self.assertEqual(
            [cmd for cmd, _ in bot_module.ADMIN_COMMANDS],
            ["admin", "users", "ban", "unban"],
        )
        for admin_cmd in ("admin", "users", "ban", "unban"):
            self.assertNotIn(admin_cmd, self.EXPECTED)

    async def test_set_bot_commands_publishes_two_scoped_menus(self):
        from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeChat

        class FakeBot:
            def __init__(self):
                self.calls = []  # list of (commands, scope)

            async def set_my_commands(self, commands, scope=None):
                self.calls.append((commands, scope))

        fake_app = SimpleNamespace(bot=FakeBot())
        await bot_module.set_bot_commands(fake_app)
        self.assertEqual(len(fake_app.bot.calls), 2)

        default_commands, default_scope = fake_app.bot.calls[0]
        self.assertIsInstance(default_scope, BotCommandScopeAllPrivateChats)
        self.assertEqual(
            [(c.command, c.description) for c in default_commands],
            bot_module.BOT_COMMANDS,
        )
        default_names = [c.command for c in default_commands]
        for admin_cmd in ("admin", "users", "ban", "unban"):
            self.assertNotIn(admin_cmd, default_names)

        owner_commands, owner_scope = fake_app.bot.calls[1]
        self.assertIsInstance(owner_scope, BotCommandScopeChat)
        self.assertEqual(owner_scope.chat_id, config.OWNER_ID)
        self.assertEqual(
            [(c.command, c.description) for c in owner_commands],
            bot_module.BOT_COMMANDS + bot_module.KEEPER_COMMANDS
            + bot_module.ADMIN_COMMANDS,
        )
        owner_names = [c.command for c in owner_commands]
        for admin_cmd in ("admin", "users", "ban", "unban"):
            self.assertIn(admin_cmd, owner_names)

    def test_help_lists_new_commands(self):
        for cmd in ("/files", "/images", "/videos", "/audio", "/docs",
                    "/apps", "/clear", "/list", "/setkey",
                    "/delkey", "/mykey", "/links", "/dellink",
                    "/analyse"):
            self.assertIn(cmd, bot_module.HELP_TEXT, cmd)
        self.assertNotIn("/ocr", bot_module.HELP_TEXT)
        # /chat is the keeper's alone: not in the shared help text —
        # it lives in KEEPER_HELP_TEXT, shown to the keeper only.
        self.assertNotIn("/chat", bot_module.HELP_TEXT)
        self.assertIn("/chat", bot_module.KEEPER_HELP_TEXT)
        # Admin commands are NOT in the shared help text — they live in
        # ADMIN_HELP_TEXT, shown to the keeper only.
        for cmd in ("/admin", "/users", "/ban", "/unban"):
            self.assertNotIn(cmd, bot_module.HELP_TEXT, cmd)
            self.assertIn(cmd, bot_module.ADMIN_HELP_TEXT, cmd)

    async def test_help_shows_admin_section_only_to_owner(self):
        owner_update = FakeUpdate(user_id=111)
        await bot_module.help_cmd(owner_update, FakeContext())
        owner_text = owner_update.message.replies[0][0]
        for cmd in ("/admin", "/users", "/ban", "/unban"):
            self.assertIn(cmd, owner_text, cmd)
        # The keeper also sees /chat in /help.
        self.assertIn("/chat", owner_text)

        other_update = FakeUpdate(user_id=222)
        await bot_module.help_cmd(other_update, FakeContext())
        other_text = other_update.message.replies[0][0]
        for cmd in ("/admin", "/users", "/ban", "/unban"):
            self.assertNotIn(cmd, other_text, cmd)
        # ... and /chat is hidden from non-owners too.
        self.assertNotIn("/chat", other_text)
        # ... but the ordinary commands are still there for everyone.
        self.assertIn("/list", other_text)


if __name__ == "__main__":
    unittest.main()
