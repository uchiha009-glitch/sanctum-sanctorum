"""Offline tests for /links and /analyse — no real Telegram calls,
no real Gemini calls.

Run from the project root:
    .venv/bin/python -m unittest discover -s tests -v

Covers:
  * links store: add/get/list/search/delete are scoped per user, the
    500/user cap, URL validation/normalisation, missing user_id is a
    TypeError (no unscoped path),
  * /links handler: save (URL + optional title, bare domain), list
    (paged), show one, search, /dellink, isolation between users,
    /clear wipes links too,
  * bare-URL plain messages are auto-saved as links; ordinary chat is
    not,
  * /analyse: text files and PDFs are summarised via Gemini (mocked);
    images are REFUSED outright — no download, no vision call
    (Nov 15, 2026, owner's decision); audio/video/archives return
    metadata + a plain explanation (no hallucinated summary, no AI
    call), oversized files are refused, BYOK refusal for a non-owner
    without a key,
  * ai.py one-shot helpers: no key means no HTTP call; oversized
    images are refused before any call,
  * the menu publishes links/dellink/analyse; /ocr is gone entirely
    (removed Nov 15, 2026) — it is in neither the Menu nor /help,
    and no handler is registered for it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Make the project root importable (bot.py, vault.py, config.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai as ai_module
import bot as bot_module
import config
import vault as vault_module
from vault import Store

OWNER = 111
USER_A = 222
USER_B = 333


# ---------------------------------------------------------------------------
# Fakes (mirroring tests/test_browse_offline.py and
# tests/test_public_offline.py)
# ---------------------------------------------------------------------------

class FakeTGFile:
    def __init__(self, data: bytes):
        self.data = data

    async def download_as_bytearray(self):
        return bytearray(self.data)


class FakeBot:
    def __init__(self, files=None):
        self.files = files or {}
        self.get_file_calls = []

    async def get_file(self, file_id):
        self.get_file_calls.append(file_id)
        if file_id not in self.files:
            raise RuntimeError("file not found")
        return FakeTGFile(self.files[file_id])

    async def forward_message(self, chat_id, from_chat_id, message_id):
        return SimpleNamespace(message_id=999)

    async def set_my_commands(self, commands, scope=None):
        self.commands_calls = getattr(self, "commands_calls", [])
        self.commands_calls.append((commands, scope))


class FakeMessage:
    def __init__(self, text="", photo=None, document=None,
                 reply_to_message=None, chat_id=OWNER, message_id=1):
        self.text = text
        self.caption = ""
        self.photo = photo
        self.document = document
        self.reply_to_message = reply_to_message
        self.chat_id = chat_id
        self.message_id = message_id
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeCallbackQuery:
    def __init__(self, data, user_id=OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answered = False
        self.edits = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class FakeUpdate:
    def __init__(self, message=None, user_id=OWNER, callback_query=None,
                 text=""):
        self.message = message if message is not None else FakeMessage(text)
        self.effective_message = self.message
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=user_id, type="private")
        self.callback_query = callback_query


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=None)
        self.bot = bot or FakeBot()


def _photo(file_id="photo1", file_size=100):
    return [SimpleNamespace(file_id=file_id, file_size=file_size)]


# ---------------------------------------------------------------------------
# Store-level link tests
# ---------------------------------------------------------------------------

class LinksStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data", owner_id=OWNER)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_normalise_url(self):
        self.assertEqual(
            Store.normalise_url("example.com"), "https://example.com")
        self.assertEqual(
            Store.normalise_url("https://example.com/a?b=c"),
            "https://example.com/a?b=c")
        self.assertEqual(
            Store.normalise_url("https://example.com."),
            "https://example.com")
        self.assertIsNone(Store.normalise_url("javascript:alert(1)"))
        self.assertIsNone(Store.normalise_url("not a url"))
        self.assertIsNone(Store.normalise_url(""))
        self.assertIsNone(Store.normalise_url("https://"))
        self.assertIsNone(Store.normalise_url("ftp://example.com"))

    def test_add_get_list_search_delete_scoped(self):
        a1 = self.store.add_link("https://alice.example/x", "Alice page",
                                 user_id=USER_A)
        a2 = self.store.add_link("bob.example", user_id=USER_A)
        b1 = self.store.add_link("https://bob.example/y", user_id=USER_B)
        self.assertEqual(a2.url, "https://bob.example")
        # Each user sees only their own links.
        links, total = self.store.list_links(user_id=USER_A)
        self.assertEqual(total, 2)
        self.assertEqual({l.id for l in links}, {a1.id, a2.id})
        links, total = self.store.list_links(user_id=USER_B)
        self.assertEqual(total, 1)
        self.assertEqual(links[0].id, b1.id)
        # Another user's link id is simply "not found".
        self.assertIsNone(self.store.get_link(a1.id, user_id=USER_B))
        self.assertEqual(self.store.get_link(a1.id, user_id=USER_A).url,
                         "https://alice.example/x")
        # Search is scoped too, and LIKE wildcards are literal.
        self.assertEqual(len(self.store.search_links("alice", user_id=USER_A)), 1)
        self.assertEqual(self.store.search_links("alice", user_id=USER_B), [])
        self.store.add_link("https://pct.example/100%", user_id=USER_A)
        self.assertEqual(len(self.store.search_links("%", user_id=USER_A)), 1)
        # Deleting another user's link does nothing; deleting your own works.
        self.assertIsNone(self.store.delete_link(a1.id, user_id=USER_B))
        self.assertIsNotNone(self.store.get_link(a1.id, user_id=USER_A))
        self.assertIsNotNone(self.store.delete_link(a1.id, user_id=USER_A))
        self.assertIsNone(self.store.get_link(a1.id, user_id=USER_A))

    def test_add_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            self.store.add_link("not a url", user_id=USER_A)
        with self.assertRaises(ValueError):
            self.store.add_link("", user_id=USER_A)

    def test_links_cap(self):
        old = vault_module.MAX_LINKS_PER_USER
        vault_module.MAX_LINKS_PER_USER = 2
        try:
            self.store.add_link("https://a.example", user_id=USER_A)
            self.store.add_link("https://b.example", user_id=USER_A)
            with self.assertRaises(ValueError):
                self.store.add_link("https://c.example", user_id=USER_A)
            # The cap is per user: USER_B still has room.
            self.store.add_link("https://c.example", user_id=USER_B)
        finally:
            vault_module.MAX_LINKS_PER_USER = old

    def test_missing_user_id_is_type_error(self):
        with self.assertRaises(TypeError):
            self.store.add_link("https://x.example")  # noqa: B026
        with self.assertRaises(TypeError):
            self.store.get_link(1)
        with self.assertRaises(TypeError):
            self.store.list_links()
        with self.assertRaises(TypeError):
            self.store.delete_link(1)

    def test_clear_all_wipes_links_per_user(self):
        self.store.add_link("https://a.example", user_id=USER_A)
        self.store.add_link("https://b.example", user_id=USER_B)
        self.store.clear_all(user_id=USER_A)
        self.assertEqual(self.store.list_links(user_id=USER_A)[1], 0)
        self.assertEqual(self.store.list_links(user_id=USER_B)[1], 1)

    def test_aggregate_stats_counts_links(self):
        self.store.add_link("https://a.example", user_id=USER_A)
        self.assertEqual(self.store.aggregate_stats()["total_links"], 1)


# ---------------------------------------------------------------------------
# Handler tests: /links, /dellink, bare-URL auto-save, /clear with links
# ---------------------------------------------------------------------------

class LinksHandlerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data", owner_id=OWNER)
        self.old_token, self.old_owner, self.old_key = (
            config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY)
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = OWNER
        config.GEMINI_API_KEY = None
        bot_module.STORE = self.store

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY = (
            self.old_token, self.old_owner, self.old_key)
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    async def _run(self, handler, user_id=OWNER, args=None, message=None,
                   bot=None):
        update = FakeUpdate(message=message, user_id=user_id)
        await handler(update, FakeContext(args=args, bot=bot))
        return update

    def _text(self, update):
        return update.message.replies[0]

    async def test_links_save_list_show_search_delete(self):
        u = await self._run(bot_module.links_cmd, args=[
            "https://example.com/page", "Example", "Page"])
        self.assertIn("Saved link #1", self._text(u))
        link = self.store.get_link(1, user_id=OWNER)
        self.assertEqual(link.url, "https://example.com/page")
        self.assertEqual(link.title, "Example Page")

        u = await self._run(bot_module.links_cmd, args=["example.org"])
        self.assertIn("Saved link #2", self._text(u))
        self.assertEqual(self.store.get_link(2, user_id=OWNER).url,
                         "https://example.org")

        u = await self._run(bot_module.links_cmd)
        self.assertIn("Your links", self._text(u))
        self.assertIn("#1", self._text(u))

        u = await self._run(bot_module.links_cmd, args=["1"])
        self.assertIn("https://example.com/page", self._text(u))

        u = await self._run(bot_module.links_cmd, args=["example"])
        self.assertIn("2 saved link(s)", self._text(u))

        u = await self._run(bot_module.links_cmd, args=["no-such-link"])
        self.assertIn("No saved links match", self._text(u))

        u = await self._run(bot_module.dellink_cmd, args=["1"])
        self.assertIn("Deleted link #1", self._text(u))
        self.assertIsNone(self.store.get_link(1, user_id=OWNER))
        u = await self._run(bot_module.dellink_cmd, args=["1"])
        self.assertIn("No saved link #1", self._text(u))

    async def test_links_empty_and_bad_id(self):
        u = await self._run(bot_module.links_cmd)
        self.assertIn("no saved links", self._text(u).lower())
        u = await self._run(bot_module.links_cmd, args=["9"])
        self.assertIn("No saved link #9", self._text(u))
        u = await self._run(bot_module.links_cmd, args=["notaurl", "x"])
        self.assertIn("No saved links match", self._text(u))

    async def test_links_isolation_between_users(self):
        await self._run(bot_module.links_cmd, user_id=USER_A,
                        args=["https://alice.example"])
        u = await self._run(bot_module.links_cmd, user_id=USER_B)
        self.assertIn("no saved links", self._text(u).lower())
        u = await self._run(bot_module.links_cmd, user_id=USER_B, args=["1"])
        self.assertIn("No saved link #1", self._text(u))
        u = await self._run(bot_module.dellink_cmd, user_id=USER_B, args=["1"])
        self.assertIn("No saved link #1", self._text(u))
        self.assertIsNotNone(self.store.get_link(1, user_id=USER_A))

    async def test_links_paging_callback(self):
        for i in range(12):
            self.store.add_link(f"https://example.com/{i}", user_id=OWNER)
        query = FakeCallbackQuery("links:1", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=query)
        await bot_module.links_page(update, FakeContext())
        self.assertIn("page 2 of 2", query.edits[0])
        self.assertIn("#1", query.edits[0])
        # A page tap from another user pages THEIR links, not the owner's.
        query_b = FakeCallbackQuery("links:0", user_id=USER_B)
        update_b = FakeUpdate(user_id=USER_B, callback_query=query_b)
        await bot_module.links_page(update_b, FakeContext())
        self.assertIn("no saved links", query_b.edits[0].lower())

    async def test_plain_text_bare_url_auto_saves(self):
        msg = FakeMessage(text="https://example.com/cool-thing")
        u = await self._run(bot_module.plain_text, message=msg)
        self.assertIn("Saved link #1", self._text(u))
        self.assertEqual(
            self.store.get_link(1, user_id=OWNER).url,
            "https://example.com/cool-thing")
        # A bare domain is auto-saved too.
        msg = FakeMessage(text="example.org")
        u = await self._run(bot_module.plain_text, message=msg)
        self.assertIn("Saved link #2", self._text(u))
        # Ordinary chat is NOT saved as a link.
        before = self.store.list_links(user_id=OWNER)[1]
        msg = FakeMessage(text="hello, how are you?")
        u = await self._run(bot_module.plain_text, message=msg)
        self.assertEqual(self.store.list_links(user_id=OWNER)[1], before)
        self.assertTrue(self._text(u))
        # A URL inside a sentence is chat, not an auto-save.
        msg = FakeMessage(text="check out https://example.com later")
        await self._run(bot_module.plain_text, message=msg)
        self.assertEqual(self.store.list_links(user_id=OWNER)[1], before)

    async def test_clear_wipes_links_and_reports_them(self):
        self.store.add_file(file_id="f1", filename="a.txt",
                            file_type="document", data=b"x", user_id=OWNER)
        self.store.add_note("a note", user_id=OWNER)
        self.store.add_link("https://a.example", user_id=OWNER)
        u = await self._run(bot_module.clear_cmd)
        self.assertIn("link(s)", self._text(u))
        query = FakeCallbackQuery("clear:yes", user_id=OWNER)
        update = FakeUpdate(user_id=OWNER, callback_query=query)
        await bot_module.clear_confirm(update, FakeContext())
        self.assertIn("Vault wiped", query.edits[0])
        self.assertIn("1 link(s)", query.edits[0])
        self.assertEqual(self.store.list_links(user_id=OWNER)[1], 0)


# ---------------------------------------------------------------------------
# AI-key resolution (used by /analyse)
# ---------------------------------------------------------------------------

class ResolveAiKeyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data", owner_id=OWNER)
        self.old_token, self.old_owner, self.old_key = (
            config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY)
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = OWNER
        config.GEMINI_API_KEY = "owner-env-key"
        bot_module.STORE = self.store

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY = (
            self.old_token, self.old_owner, self.old_key)
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def test_owner_gets_env_key_only(self):
        key, user_key = bot_module._resolve_ai_key(OWNER)
        self.assertEqual((key, user_key), ("owner-env-key", False))

    def test_owner_without_env_key_gets_nothing(self):
        config.GEMINI_API_KEY = None
        key, _ = bot_module._resolve_ai_key(OWNER)
        self.assertIsNone(key)

    def test_non_owner_never_gets_owner_key(self):
        key, user_key = bot_module._resolve_ai_key(USER_A)
        self.assertIsNone(key)
        # …unless they linked their own.
        self.store.set_user_key(USER_A, "user-own-key-1234567890")
        key, user_key = bot_module._resolve_ai_key(USER_A)
        self.assertEqual((key, user_key), ("user-own-key-1234567890", True))

    def test_no_user_no_key(self):
        self.assertEqual(bot_module._resolve_ai_key(None), (None, False))


# ---------------------------------------------------------------------------
# /analyse handler tests (AI mocked, downloads faked)
# ---------------------------------------------------------------------------

class AnalyseTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data", owner_id=OWNER)
        self.old_token, self.old_owner, self.old_key = (
            config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY)
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = OWNER
        config.GEMINI_API_KEY = None
        bot_module.STORE = self.store
        self.bot = FakeBot(files={
            "photo1": b"FAKE-IMAGE-BYTES",
            "txt1": b"hello world\nthis is a test file",
            "pdf1": b"%PDF-fake",
            "apk1": b"PK\x03\x04binary",
        })
        self.text_ai = mock.AsyncMock(return_value="SUMMARY: hello world")
        self._p2 = mock.patch.object(bot_module, "analyze_text_with_ai",
                                     self.text_ai)
        self._p2.start()

    def tearDown(self):
        self._p2.stop()
        config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY = (
            self.old_token, self.old_owner, self.old_key)
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def _seed_vault_file(self, filename, file_type, file_id, size=100,
                         user_id=OWNER):
        return self.store.add_file(
            file_id=file_id, filename=filename, file_type=file_type,
            size=size, telegram_file_id=file_id,
            source_chat_id=user_id, source_message_id=1, user_id=user_id)

    async def _run(self, handler, user_id=OWNER, args=None, message=None):
        update = FakeUpdate(message=message, user_id=user_id)
        await handler(update, FakeContext(args=args, bot=self.bot))
        return update

    def _text(self, update):
        return update.message.replies[0]

    # -- /analyse ------------------------------------------------------

    async def test_analyse_text_file_summarised(self):
        config.GEMINI_API_KEY = "owner-env-key"
        self._seed_vault_file("notes.txt", "document", "txt1")
        u = await self._run(bot_module.analyse_cmd, args=["1"])
        self.assertIn("SUMMARY: hello world", self._text(u))
        self.assertIn("notes.txt", self._text(u))
        _, kwargs = self.text_ai.call_args
        self.assertEqual(kwargs["api_key"], "owner-env-key")
        prompt = self.text_ai.call_args.args[1]
        self.assertIn("hello world", prompt)
        self.assertIn("this is a test file", prompt)

    async def test_analyse_pdf_uses_extracted_text(self):
        config.GEMINI_API_KEY = "owner-env-key"
        self._seed_vault_file("doc.pdf", "document", "pdf1")
        with mock.patch.object(bot_module, "_pdf_to_text",
                               return_value="PDF CONTENT HERE"):
            u = await self._run(bot_module.analyse_cmd, args=["1"])
        self.assertIn("SUMMARY", self._text(u))
        prompt = self.text_ai.call_args.args[1]
        self.assertIn("PDF CONTENT HERE", prompt)

    async def test_analyse_image_refused_no_vision_no_download(self):
        # Images are refused outright (Nov 15, 2026): no download, no
        # vision call, no key needed — even the owner WITH a key.
        self._seed_vault_file("pic.jpg", "photo", "photo1")
        u = await self._run(bot_module.analyse_cmd, args=["1"])
        text = self._text(u)
        self.assertIn("no longer read images", text)
        self.assertIn("pic.jpg", text)
        self.text_ai.assert_not_called()
        self.assertEqual(self.bot.get_file_calls, [])
        # bot.py no longer imports the vision helper at all, so no
        # command path can reach it.
        self.assertFalse(hasattr(bot_module, "analyze_image_with_ai"))

    async def test_analyse_document_image_refused_too(self):
        # An image sent as a document is still an image — same refusal.
        self._seed_vault_file("pic.png", "document", "photo1")
        u = await self._run(bot_module.analyse_cmd, args=["1"])
        self.assertIn("no longer read images", self._text(u))
        self.text_ai.assert_not_called()
        self.assertEqual(self.bot.get_file_calls, [])

    async def test_analyse_unsupported_returns_metadata_no_ai(self):
        config.GEMINI_API_KEY = "owner-env-key"
        self._seed_vault_file("app.apk", "document", "apk1")
        u = await self._run(bot_module.analyse_cmd, args=["1"])
        text = self._text(u)
        self.assertIn("app.apk", text)
        self.assertIn("can't analyse", text)
        self.text_ai.assert_not_called()
        self.assertEqual(self.bot.get_file_calls, [])

    async def test_analyse_non_owner_without_key_refused(self):
        config.GEMINI_API_KEY = "owner-env-key"
        self._seed_vault_file("notes.txt", "document", "txt1",
                              user_id=USER_A)
        u = await self._run(bot_module.analyse_cmd, user_id=USER_A,
                            args=["1"])
        self.assertIn("/setkey", self._text(u))
        self.text_ai.assert_not_called()
        self.assertEqual(self.bot.get_file_calls, [])

    async def test_analyse_oversized_file_refused(self):
        config.GEMINI_API_KEY = "owner-env-key"
        self._seed_vault_file("big.txt", "document", "txt1",
                              size=25 * 1024 * 1024)
        u = await self._run(bot_module.analyse_cmd, args=["1"])
        self.assertIn("too large", self._text(u))
        self.text_ai.assert_not_called()
        self.assertEqual(self.bot.get_file_calls, [])

    async def test_analyse_no_args_usage(self):
        u = await self._run(bot_module.analyse_cmd)
        self.assertIn("Usage", self._text(u))

    async def test_analyse_other_users_file_is_not_found(self):
        config.GEMINI_API_KEY = "owner-env-key"
        self._seed_vault_file("notes.txt", "document", "txt1",
                              user_id=USER_A)
        u = await self._run(bot_module.analyse_cmd, user_id=USER_B,
                            args=["1"])
        self.assertIn("No file matches", self._text(u))
        self.text_ai.assert_not_called()

    # -- text extraction helpers ---------------------------------------

    def test_extract_text_pdf_without_pdftotext(self):
        with mock.patch.object(bot_module.shutil, "which",
                               return_value=None):
            self.assertIsNone(bot_module._pdf_to_text(b"%PDF-fake"))

    def test_extract_text_rejects_binary(self):
        self.assertIsNone(
            bot_module._extract_text(b"PK\x03\x04\x00binary", "app.apk"))
        self.assertEqual(
            bot_module._extract_text(b"plain text", "notes.txt"),
            "plain text")


# ---------------------------------------------------------------------------
# ai.py one-shot helpers
# ---------------------------------------------------------------------------

class AiOneShotTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_image_no_key_never_calls_api(self):
        with mock.patch.object(ai_module, "_generate_with_contents",
                               new_callable=mock.AsyncMock) as gen:
            reply = await ai_module.analyze_image_with_ai(
                OWNER, b"bytes", "read this", api_key=None)
            gen.assert_not_called()
        self.assertIn("no Gemini key", reply)

    async def test_image_oversized_never_calls_api(self):
        with mock.patch.object(ai_module, "_generate_with_contents",
                               new_callable=mock.AsyncMock) as gen:
            reply = await ai_module.analyze_image_with_ai(
                OWNER, b"x" * (ai_module.MAX_IMAGE_BYTES + 1), "read this",
                api_key="some-key")
            gen.assert_not_called()
        self.assertIn("too large", reply)

    async def test_image_success_uses_vision_contents(self):
        with mock.patch.object(ai_module, "_generate_with_contents",
                               new_callable=mock.AsyncMock,
                               return_value="VISION OK") as gen:
            reply = await ai_module.analyze_image_with_ai(
                OWNER, b"img", "read this", api_key="k",
                mime_type="image/png")
        self.assertEqual(reply, "VISION OK")
        contents = gen.call_args.args[2]
        part = contents[0]["parts"][1]
        self.assertEqual(part["inline_data"]["mime_type"], "image/png")
        self.assertTrue(part["inline_data"]["data"])

    async def test_text_no_key_never_calls_api(self):
        with mock.patch.object(ai_module, "_generate_with_contents",
                               new_callable=mock.AsyncMock) as gen:
            reply = await ai_module.analyze_text_with_ai(
                OWNER, "summarise", api_key=None)
            gen.assert_not_called()
        self.assertIn("no Gemini key", reply)

    async def test_text_falls_back_on_404(self):
        calls = []

        async def flaky(api_key, model, contents, system_prompt=None):
            calls.append(model)
            if len(calls) == 1:
                raise RuntimeError("404 model not found: gemini-3.5-flash-lite")
            return "FALLBACK OK"

        with mock.patch.object(ai_module, "_generate_with_contents",
                               side_effect=flaky):
            reply = await ai_module.analyze_text_with_ai(
                OWNER, "summarise", api_key="k")
        self.assertEqual(reply, "FALLBACK OK")
        self.assertEqual(calls, [ai_module.MODEL_PRIMARY,
                                 ai_module.MODEL_FALLBACK])


# ---------------------------------------------------------------------------
# Menu publishes the three restored commands
# ---------------------------------------------------------------------------

class MenuRestoredCommandsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.old_token, self.old_owner = config.BOT_TOKEN, config.OWNER_ID
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = OWNER
        bot_module.STORE = self.store

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID = self.old_token, self.old_owner
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def test_restored_commands_in_menu_and_help(self):
        names = [cmd for cmd, _ in bot_module.BOT_COMMANDS]
        for cmd in ("links", "analyse"):
            self.assertIn(cmd, names)
            self.assertIn(f"/{cmd}", bot_module.HELP_TEXT)
        # /ocr was removed entirely (Nov 15, 2026): no menu entry, no
        # /help line, no handler function left in bot.py.
        self.assertNotIn("ocr", names)
        self.assertNotIn("/ocr", bot_module.HELP_TEXT)
        self.assertFalse(hasattr(bot_module, "ocr_cmd"))
        # /dellink is a real command, but typed-only now: it works when
        # typed and is listed in /help, it is just not on the short Menu.
        self.assertNotIn("dellink", names)
        self.assertIn("/dellink", bot_module.HELP_TEXT)
        # Still no admin commands in the default menu.
        for admin_cmd in ("admin", "users", "ban", "unban"):
            self.assertNotIn(admin_cmd, names)

    async def test_set_bot_commands_publishes_restored_commands(self):
        fake_bot = FakeBot()
        fake_bot.commands_calls = []
        fake_app = SimpleNamespace(bot=fake_bot)
        await bot_module.set_bot_commands(fake_app)
        default_commands, _ = fake_bot.commands_calls[0]
        names = [c.command for c in default_commands]
        for cmd in ("links", "analyse"):
            self.assertIn(cmd, names)
        self.assertNotIn("ocr", names)
        self.assertNotIn("dellink", names)


if __name__ == "__main__":
    unittest.main()
