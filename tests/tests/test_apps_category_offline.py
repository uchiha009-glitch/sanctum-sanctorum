"""Offline tests for the "apps" category — APKs/archives filed by
filename extension, not as documents. No real Telegram calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers (the "it considers apk as doc" bug):
  * add_file files an APK / archive under category "apps" (any case),
    while plain documents stay "documents" and photo/video/audio keep
    their own categories,
  * the on-disk path of a saved APK lives under data/apps/,
  * opening a legacy database re-files old "documents" rows whose
    filename ends in an app/archive extension to "apps" — and leaves
    plain documents (and "photo.apk.jpg") alone,
  * list_files category="apps" returns only APKs/archives and
    category="documents" no longer contains them,
  * /docs (bot view) excludes APKs/archives and /apps lists them,
  * the save confirmation says "(apps, ...)" for an APK and
    "(documents, ...)" for a plain document,
  * /stats shows the apps bucket, and per-user numbering is unchanged
    (one user's APKs do not affect another user's numbers).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

# Make the project root importable (bot.py, vault.py, config.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as bot_module
import config
from vault import APP_ARCHIVE_EXTENSIONS, Store, classify_category


class FakeMessage:
    """Stands in for telegram.Message in save/category paths."""

    def __init__(self, document=None, photo=None, video=None, audio=None,
                 voice=None, caption="", chat_id=111, message_id=1,
                 date=None):
        self.text = ""
        self.caption = caption
        self.document = document
        self.photo = photo
        self.video = video
        self.audio = audio
        self.voice = voice
        self.chat_id = chat_id
        self.message_id = message_id
        self.date = date or datetime.now(timezone.utc)
        self.replies = []       # reply_text calls: (text, kwargs)
        self.sent_files = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))

    async def reply_document(self, document, **kwargs):
        self.sent_files.append(("document", {"document": document, **kwargs}))

    async def reply_photo(self, photo, **kwargs):
        self.sent_files.append(("photo", {"photo": photo, **kwargs}))

    async def reply_video(self, video, **kwargs):
        self.sent_files.append(("video", {"video": video, **kwargs}))

    async def reply_audio(self, audio, **kwargs):
        self.sent_files.append(("audio", {"audio": audio, **kwargs}))

    async def reply_voice(self, voice, **kwargs):
        self.sent_files.append(("voice", {"voice": voice, **kwargs}))


class FakeUpdate:
    def __init__(self, message, user_id=111):
        self.message = message
        self.effective_message = message
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=user_id)


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=None)
        self.bot = None


def _tg_file(file_id, file_name=None, file_size=0):
    return SimpleNamespace(file_id=file_id, file_name=file_name,
                           file_size=file_size)


class ClassifyTestCase(unittest.TestCase):
    def test_apk_and_archives_are_apps(self):
        for name in ("Vibes.apk", "GAME.APK", "mod.xapk", "a.apks",
                     "b.apkm", "SUJATHA SIMHADRI.zip", "data.rar",
                     "c.7z", "d.tar", "e.tar.gz", "f.bz2"):
            self.assertEqual(
                classify_category(name, "document"), "apps", name)

    def test_plain_documents_stay_documents(self):
        for name in ("notes.pdf", "report.txt", "photo.apk.jpg",
                     "avatar", "my.tar.backup.txt"):
            self.assertEqual(
                classify_category(name, "document"), "documents", name)

    def test_other_kinds_keep_their_category(self):
        self.assertEqual(classify_category("pic.jpg", "photo"), "photos")
        self.assertEqual(classify_category("clip.mp4", "video"), "videos")
        self.assertEqual(classify_category("song.mp3", "audio"), "audio")
        self.assertEqual(classify_category("note.ogg", "voice"), "audio")
        self.assertEqual(classify_category("weird", "mystery"), "documents")


class StoreAppsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_apk_is_apps(self):
        rec = self.store.add_file(
            file_id="tg-apk", filename="Vibes.apk", file_type="document",
            size=115, telegram_file_id="tg-apk",
            source_chat_id=111, source_message_id=5, user_id=111)
        self.assertEqual(rec.category, "apps")
        self.assertEqual(rec.display().split("(")[1].split(",")[0], "apps")

    def test_add_archives_are_apps(self):
        for name in ("backup.zip", "data.rar", "mod.xapk"):
            rec = self.store.add_file(
                file_id=f"tg-{name}", filename=name, file_type="document",
                size=10, telegram_file_id=f"tg-{name}",
                source_chat_id=111, source_message_id=1, user_id=111)
            self.assertEqual(rec.category, "apps", name)

    def test_add_plain_doc_stays_documents(self):
        rec = self.store.add_file(
            file_id="tg-pdf", filename="notes.pdf", file_type="document",
            size=10, telegram_file_id="tg-pdf",
            source_chat_id=111, source_message_id=1, user_id=111)
        self.assertEqual(rec.category, "documents")

    def test_on_disk_apk_lives_under_apps(self):
        rec = self.store.add_file(
            file_id="tg-apk", filename="app.apk", file_type="document",
            data=b"PK\x03\x04", user_id=111)
        self.assertEqual(rec.category, "apps")
        self.assertEqual(rec.path.parent.name, "apps")
        self.assertTrue(rec.path.exists())

    def test_list_files_by_category(self):
        self.store.add_file(
            file_id="tg-1", filename="Vibes.apk", file_type="document",
            size=10, telegram_file_id="tg-1",
            source_chat_id=111, source_message_id=1, user_id=111)
        self.store.add_file(
            file_id="tg-2", filename="notes.pdf", file_type="document",
            size=10, telegram_file_id="tg-2",
            source_chat_id=111, source_message_id=2, user_id=111)
        apps, total = self.store.list_files(category="apps", user_id=111)
        self.assertEqual(total, 1)
        self.assertEqual(apps[0].filename, "Vibes.apk")
        docs, total = self.store.list_files(
            category="documents", user_id=111)
        self.assertEqual(total, 1)
        self.assertEqual(docs[0].filename, "notes.pdf")

    def test_stats_has_apps_bucket(self):
        self.store.add_file(
            file_id="tg-1", filename="Vibes.apk", file_type="document",
            size=100, telegram_file_id="tg-1",
            source_chat_id=111, source_message_id=1, user_id=111)
        self.store.add_file(
            file_id="tg-2", filename="a.zip", file_type="document",
            size=50, telegram_file_id="tg-2",
            source_chat_id=111, source_message_id=2, user_id=111)
        self.store.add_file(
            file_id="tg-3", filename="notes.pdf", file_type="document",
            size=10, telegram_file_id="tg-3",
            source_chat_id=111, source_message_id=3, user_id=111)
        s = self.store.stats(user_id=111)
        self.assertEqual(s["by_category"]["apps"]["count"], 2)
        self.assertEqual(s["by_category"]["apps"]["size"], 150)
        self.assertEqual(s["by_category"]["documents"]["count"], 1)

    def test_per_user_numbering_unchanged_by_apps(self):
        a = self.store.add_file(
            file_id="tg-1", filename="Vibes.apk", file_type="document",
            size=10, telegram_file_id="tg-1",
            source_chat_id=111, source_message_id=1, user_id=111)
        b = self.store.add_file(
            file_id="tg-2", filename="CapCut.apk", file_type="document",
            size=10, telegram_file_id="tg-2",
            source_chat_id=222, source_message_id=1, user_id=222)
        self.assertEqual(a.user_num, 1)
        self.assertEqual(b.user_num, 1)  # other user's numbering is separate


class MigrationAppsTestCase(unittest.TestCase):
    """A database saved before the fix has the APKs as 'documents';
    opening it re-files them under 'apps' — the live #2 zip and #5 apk."""

    def _legacy_db(self, rows):
        tmp = tempfile.TemporaryDirectory()
        data_dir = Path(tmp.name) / "data"
        data_dir.mkdir(parents=True)
        conn = sqlite3.connect(data_dir / "vault.db")
        conn.executescript(
            "CREATE TABLE files ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id INTEGER, user_num INTEGER,"
            " filename TEXT NOT NULL, stored_name TEXT NOT NULL DEFAULT '',"
            " category TEXT NOT NULL, file_type TEXT NOT NULL,"
            " size INTEGER NOT NULL DEFAULT 0, file_id TEXT NOT NULL,"
            " caption TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,"
            " telegram_file_id TEXT NOT NULL DEFAULT '',"
            " source_chat_id INTEGER, source_message_id INTEGER);"
            "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id INTEGER, text TEXT NOT NULL,"
            " created_at INTEGER NOT NULL);"
            "CREATE TABLE links (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id INTEGER, url TEXT NOT NULL,"
            " title TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);"
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY,"
            " api_key TEXT NOT NULL DEFAULT '',"
            " is_banned INTEGER NOT NULL DEFAULT 0,"
            " created_at INTEGER NOT NULL);"
        )
        for row_id, filename in rows:
            conn.execute(
                "INSERT INTO files (id, user_id, user_num, filename,"
                " category, file_type, size, file_id, caption, created_at,"
                " telegram_file_id, source_chat_id, source_message_id)"
                " VALUES (?, 111, ?, ?, 'documents', 'document', 10,"
                " ?, '', 1700000000, ?, NULL, NULL)",
                (row_id, row_id, filename, f"tg-{row_id}", f"tg-{row_id}"),
            )
        conn.commit()
        conn.close()
        return tmp, data_dir

    def test_migration_refiles_apk_and_zip(self):
        rows = [
            (1, "video.mp4"),  # file_type is document here, name decides
            (2, "SUJATHA SIMHADRI.zip"),
            (3, "RDR.mp3"),
            (4, "photo.jpg"),
            (5, "Vibes.apk"),
            (6, "reddit .txt"),
            (7, "photo.apk.jpg"),
        ]
        tmp, data_dir = self._legacy_db(rows)
        try:
            store = Store(data_dir, owner_id=111)
            try:
                apps, total = store.list_files(category="apps", user_id=111)
                self.assertEqual(total, 2)
                self.assertEqual(
                    sorted(r.filename for r in apps),
                    ["SUJATHA SIMHADRI.zip", "Vibes.apk"])
                docs, total = store.list_files(
                    category="documents", user_id=111)
                self.assertEqual(total, 5)
                # The misleading names stay documents: they are not apps.
                self.assertIn("photo.apk.jpg",
                              [r.filename for r in docs])
                self.assertIn("video.mp4",
                              [r.filename for r in docs])
                # User numbers are untouched by the re-filing.
                self.assertEqual(
                    store.get_file(5, user_id=111).filename, "Vibes.apk")
                self.assertEqual(
                    store.get_file(5, user_id=111).category, "apps")
            finally:
                store.close()
            # Idempotent: reopening changes nothing.
            store = Store(data_dir, owner_id=111)
            try:
                apps, total = store.list_files(category="apps", user_id=111)
                self.assertEqual(total, 2)
            finally:
                store.close()
        finally:
            tmp.cleanup()


class BotAppsViewTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.old_token, self.old_owner = config.BOT_TOKEN, config.OWNER_ID
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = 111
        bot_module.STORE = self.store
        self.store.add_file(
            file_id="tg-1", filename="Vibes.apk", file_type="document",
            size=10, telegram_file_id="tg-1",
            source_chat_id=111, source_message_id=1, user_id=111)
        self.store.add_file(
            file_id="tg-2", filename="notes.pdf", file_type="document",
            size=10, telegram_file_id="tg-2",
            source_chat_id=111, source_message_id=2, user_id=111)

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID = self.old_token, self.old_owner
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    async def _run(self, handler, user_id=111, **kwargs):
        update = FakeUpdate(FakeMessage(), user_id=user_id, **kwargs)
        await handler(update, FakeContext())
        return update

    def _text(self, update):
        return update.message.replies[0][0]

    async def test_docs_view_excludes_apk(self):
        text = self._text(await self._run(bot_module.docs_cmd))
        self.assertIn("notes.pdf", text)
        self.assertNotIn("Vibes.apk", text)

    async def test_apps_view_lists_apk(self):
        text = self._text(await self._run(bot_module.apps_cmd))
        self.assertIn("Vibes.apk", text)
        self.assertNotIn("notes.pdf", text)

    async def test_save_confirmation_says_apps(self):
        doc = _tg_file("tg-new", "Fresh.apk", 12345)
        message = FakeMessage(document=doc, chat_id=111, message_id=9)
        await bot_module.save_file(
            FakeUpdate(message), FakeContext())
        text = message.replies[0][0]
        self.assertIn("Fresh.apk", text)
        self.assertIn("(apps, ", text)
        self.assertNotIn("(documents, ", text)

    async def test_save_confirmation_says_documents_for_pdf(self):
        doc = _tg_file("tg-pdf", "manual.pdf", 100)
        message = FakeMessage(document=doc, chat_id=111, message_id=10)
        await bot_module.save_file(
            FakeUpdate(message), FakeContext())
        text = message.replies[0][0]
        self.assertIn("(documents, ", text)
        self.assertNotIn("(apps, ", text)

    async def test_stats_view_shows_apps_line(self):
        update = await self._run(bot_module.stats_cmd)
        text = self._text(update)
        self.assertIn("apps (APKs & archives): 1 file(s)", text)
        self.assertIn("documents: 1 file(s)", text)


if __name__ == "__main__":
    unittest.main()
