"""Offline tests for the file_id vault storage — no real Telegram calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers:
  (a) receiving a file stores its file_id + source message reference and
      does NOT download anything (get_file is never called),
  (b) receiving a >50 MB file is accepted (no 20 MB rejection) and stored,
      with a note that it will come back by forwarding,
  (c) /get for a small file sends it back by file_id (right method per type),
  (d) /get for a large file (>50 MB) forwards the original message,
  (e) /get for a large file whose forward fails replies with the file's
      details (name, size, date) and says plainly that clearing the
      chat strands it — the old "find it in chat history" line is gone,
      because after a clear there is no history to find it in,
  (f) /get for an old on-disk record still serves the file from disk,
  (g) /list shows the date each file was added,
  plus: the migration adds the new columns to a legacy database without
  touching its rows, and delete/clear work on reference-only records.
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
from vault import Store

BIG = 110 * 1024 * 1024  # 110 MB, like the APK that was rejected before


class FakeMessage:
    """Stands in for telegram.Message in save/get paths."""

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
        self.sent_files = []    # reply_document/photo/video/audio/voice

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))

    async def _record_send(self, kind, **kwargs):
        # Read an uploaded file object now: the real handler closes it
        # right after this call returns.
        data = kwargs.get("document") or kwargs.get("photo") \
            or kwargs.get("video") or kwargs.get("audio") \
            or kwargs.get("voice")
        if hasattr(data, "read"):
            kwargs["bytes"] = data.read()
        self.sent_files.append((kind, kwargs))

    async def reply_document(self, document, **kwargs):
        await self._record_send("document", document=document, **kwargs)

    async def reply_photo(self, photo, **kwargs):
        await self._record_send("photo", photo=photo, **kwargs)

    async def reply_video(self, video, **kwargs):
        await self._record_send("video", video=video, **kwargs)

    async def reply_audio(self, audio, **kwargs):
        await self._record_send("audio", audio=audio, **kwargs)

    async def reply_voice(self, voice, **kwargs):
        await self._record_send("voice", voice=voice, **kwargs)


class FakeBot:
    """Stands in for telegram.Bot. get_file must never be called (the bot
    no longer downloads anything); forward_message is recorded."""

    def __init__(self, forward_fails=False):
        self.forwarded = []  # (chat_id, from_chat_id, message_id)
        self.forward_fails = forward_fails

    async def get_file(self, file_id):
        raise AssertionError(
            f"get_file({file_id}) was called — the bot must not download"
        )

    async def forward_message(self, chat_id, from_chat_id, message_id):
        if self.forward_fails:
            raise RuntimeError("message to forward not found")
        self.forwarded.append((chat_id, from_chat_id, message_id))
        return SimpleNamespace(message_id=999)


class FakeUpdate:
    def __init__(self, message, user_id=111):
        self.message = message
        self.effective_message = message
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=user_id)


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=None)
        self.bot = bot or FakeBot()


def _tg_file(file_id, file_name=None, file_size=0):
    return SimpleNamespace(file_id=file_id, file_name=file_name,
                           file_size=file_size)


class FileIdTestCase(unittest.IsolatedAsyncioTestCase):
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

    def _seed_reference(self, file_id="tg-1", filename="doc.pdf",
                        file_type="document", size=1000,
                        source_chat_id=111, source_message_id=10):
        return self.store.add_file(
            file_id=file_id, filename=filename, file_type=file_type,
            size=size, telegram_file_id=file_id,
            source_chat_id=source_chat_id, source_message_id=source_message_id, user_id=111)

    # -- (a) receiving a file stores the reference, downloads nothing ------

    async def test_save_stores_reference_without_downloading(self):
        bot = FakeBot()
        doc = _tg_file("tg-doc-1", "report.pdf", 2048)
        message = FakeMessage(document=doc, caption="exam",
                              chat_id=111, message_id=55)
        await bot_module.save_file(
            FakeUpdate(message), FakeContext(bot=bot)
        )
        self.assertIn("Stored #1", message.replies[0][0])
        self.assertIn("report.pdf", message.replies[0][0])
        rec = self.store.get_file(1, user_id=111)
        self.assertEqual(rec.telegram_file_id, "tg-doc-1")
        self.assertEqual(rec.source_chat_id, 111)
        self.assertEqual(rec.source_message_id, 55)
        self.assertEqual(rec.caption, "exam")
        self.assertTrue(rec.stored_by_reference)
        # Nothing was downloaded or written to disk (get_file would have
        # raised; the data dir holds only the index and the key secret).
        data_dir = Path(self.tmp.name) / "data"
        stored = [p for p in data_dir.rglob("*")
                  if p.is_file() and p.name not in ("vault.db", "key_secret")]
        self.assertEqual(stored, [])

    async def test_save_photo_uses_largest_size(self):
        small = _tg_file("tg-small", file_size=1000)
        large = _tg_file("tg-large", file_size=9000)
        message = FakeMessage(photo=[small, large], chat_id=111,
                              message_id=56)
        await bot_module.save_file(
            FakeUpdate(message), FakeContext(bot=FakeBot())
        )
        rec = self.store.get_file(1, user_id=111)
        self.assertEqual(rec.telegram_file_id, "tg-large")
        self.assertEqual(rec.file_type, "photo")
        self.assertTrue(rec.filename.startswith("photo_"))

    # -- (b) a >50 MB file is accepted, with the forwarding note -----------

    async def test_save_large_file_accepted(self):
        doc = _tg_file("tg-apk", "vibes.apk", BIG)
        message = FakeMessage(document=doc, chat_id=111, message_id=57)
        await bot_module.save_file(
            FakeUpdate(message), FakeContext(bot=FakeBot())
        )
        text = message.replies[0][0]
        self.assertIn("Stored #1", text)
        self.assertIn("vibes.apk", text)
        self.assertNotIn("can only download", text)
        self.assertIn("large file", text)
        self.assertIn("forward", text)
        rec = self.store.get_file(1, user_id=111)
        self.assertEqual(rec.size, BIG)
        self.assertEqual(rec.telegram_file_id, "tg-apk")
        self.assertEqual(rec.source_message_id, 57)

    # -- (c) /get for a small file sends it by file_id ----------------------

    async def test_get_small_file_sends_by_file_id(self):
        self._seed_reference(file_id="tg-doc", filename="doc.pdf",
                             file_type="document", size=1000)
        self._seed_reference(file_id="tg-pic", filename="pic.jpg",
                             file_type="photo", size=2000)
        self._seed_reference(file_id="tg-voice", filename="voice.ogg",
                             file_type="voice", size=3000)
        bot = FakeBot()

        msg = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg), FakeContext(args=["1"], bot=bot)
        )
        self.assertEqual(len(msg.sent_files), 1)
        kind, kwargs = msg.sent_files[0]
        self.assertEqual(kind, "document")
        self.assertEqual(kwargs["document"], "tg-doc")
        self.assertEqual(kwargs["filename"], "doc.pdf")
        self.assertEqual(bot.forwarded, [])

        msg2 = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg2), FakeContext(args=["2"], bot=bot)
        )
        kind, kwargs = msg2.sent_files[0]
        self.assertEqual(kind, "photo")
        self.assertEqual(kwargs["photo"], "tg-pic")

        msg3 = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg3), FakeContext(args=["3"], bot=bot)
        )
        kind, kwargs = msg3.sent_files[0]
        self.assertEqual(kind, "voice")
        self.assertEqual(kwargs["voice"], "tg-voice")

    # -- (d) /get for a large file forwards the original message -----------

    async def test_get_large_file_forwards_original_message(self):
        rec = self._seed_reference(
            file_id="tg-apk", filename="vibes.apk", file_type="document",
            size=BIG, source_chat_id=111, source_message_id=77,
        )
        bot = FakeBot()
        msg = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg), FakeContext(args=[str(rec.user_num)], bot=bot)
        )
        self.assertEqual(bot.forwarded, [(111, 111, 77)])
        self.assertEqual(msg.sent_files, [])
        self.assertEqual(msg.replies, [])

    # -- (e) forward failure -> file details (name, size, date) ------------

    async def test_get_large_file_forward_failure_shows_details(self):
        rec = self._seed_reference(
            file_id="tg-apk", filename="vibes.apk", file_type="document",
            size=BIG, source_chat_id=111, source_message_id=77,
        )
        bot = FakeBot(forward_fails=True)
        msg = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg), FakeContext(args=[str(rec.user_num)], bot=bot)
        )
        self.assertEqual(bot.forwarded, [])
        self.assertEqual(len(msg.replies), 1)
        text = msg.replies[0][0]
        self.assertIn("vibes.apk", text)
        self.assertIn("110.0 MB", text)
        self.assertIn(rec.date_str, text)
        self.assertIn("too large", text)
        self.assertIn("cleared this chat", text)
        self.assertIn("can't retrieve this file anymore", text)

    # -- (f) old on-disk records still serve from disk ----------------------

    async def test_get_old_disk_record_serves_from_disk(self):
        rec = self.store.add_file(
            file_id="tg-old", filename="old.txt", file_type="document",
            data=b"legacy bytes", user_id=111)
        self.assertEqual(rec.telegram_file_id, "")
        msg = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg), FakeContext(args=[str(rec.user_num)], bot=FakeBot())
        )
        self.assertEqual(len(msg.sent_files), 1)
        kind, kwargs = msg.sent_files[0]
        self.assertEqual(kind, "document")
        self.assertEqual(kwargs["bytes"], b"legacy bytes")
        self.assertEqual(kwargs["filename"], "old.txt")

    async def test_get_missing_disk_file_reports_it(self):
        rec = self.store.add_file(
            file_id="tg-old", filename="gone.txt", file_type="document",
            data=b"x", user_id=111)
        rec.path.unlink()
        msg = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg), FakeContext(args=[str(rec.user_num)], bot=FakeBot())
        )
        self.assertIn("missing from disk", msg.replies[0][0])

    # -- (g) /list shows the date each file was added -----------------------

    async def test_list_shows_date(self):
        rec = self._seed_reference(filename="doc.pdf")
        msg = FakeMessage()
        await bot_module.list_cmd(FakeUpdate(msg), FakeContext())
        text = msg.replies[0][0]
        self.assertIn("doc.pdf", text)
        self.assertIn(rec.date_str, text)

    async def test_other_user_cannot_see_or_get_my_files(self):
        # User 111 stores a file; user 222 must not see it or get it —
        # asking for it is simply "not found", with no leak that it exists.
        doc = _tg_file("tg-mine", "mine.pdf", 10)
        msg = FakeMessage(document=doc, chat_id=111, message_id=1)
        await bot_module.save_file(
            FakeUpdate(msg, user_id=111), FakeContext(bot=FakeBot())
        )
        self.assertIn("Stored #1", msg.replies[0][0])
        msg2 = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg2, user_id=222),
            FakeContext(args=["1"], bot=FakeBot()),
        )
        self.assertIn("No file matches", msg2.replies[0][0])
        self.assertEqual(msg2.sent_files, [])
        msg3 = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg3, user_id=222),
            FakeContext(args=["mine.pdf"], bot=FakeBot()),
        )
        self.assertIn("No file matches", msg3.replies[0][0])
        # But 222 CAN store and get their own file.
        doc2 = _tg_file("tg-theirs", "theirs.pdf", 20)
        msg4 = FakeMessage(document=doc2, chat_id=222, message_id=2)
        await bot_module.save_file(
            FakeUpdate(msg4, user_id=222), FakeContext(bot=FakeBot())
        )
        self.assertIn("Stored #1", msg4.replies[0][0])
        msg5 = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg5, user_id=222),
            FakeContext(args=["1"], bot=FakeBot()),
        )
        self.assertEqual(len(msg5.sent_files), 1)
        self.assertEqual(msg5.sent_files[0][1]["document"], "tg-theirs")


class MigrationTestCase(unittest.TestCase):
    """Opening a database created by the old download-to-disk version
    adds the new columns and leaves its rows intact and readable."""

    LEGACY_SCHEMA = """
    CREATE TABLE files (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        filename    TEXT NOT NULL,
        stored_name TEXT NOT NULL DEFAULT '',
        category    TEXT NOT NULL,
        file_type   TEXT NOT NULL,
        size        INTEGER NOT NULL DEFAULT 0,
        file_id     TEXT NOT NULL,
        caption     TEXT NOT NULL DEFAULT '',
        created_at  INTEGER NOT NULL
    );
    CREATE TABLE notes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        text       TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    """

    def test_migrate_legacy_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            db_path = data_dir / "vault.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(self.LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO files (filename, stored_name, category,"
                " file_type, size, file_id, caption, created_at)"
                " VALUES ('old.txt', '1_old.txt', 'documents', 'document',"
                " 5, 'tg-old', '', 1763200000)"
            )
            conn.commit()
            conn.close()

            store = Store(data_dir, owner_id=111)
            try:
                cols = {
                    r[1] for r in
                    store._conn.execute("PRAGMA table_info(files)").fetchall()
                }
                self.assertIn("telegram_file_id", cols)
                self.assertIn("source_chat_id", cols)
                self.assertIn("source_message_id", cols)
                self.assertIn("user_id", cols)
                rec = store.get_file(1, user_id=111)
                self.assertEqual(rec.filename, "old.txt")
                # The legacy row belonged to the owner: the migration
                # assigned it to the owner id.
                self.assertEqual(rec.user_id, 111)
                self.assertEqual(rec.telegram_file_id, "")
                self.assertIsNone(rec.source_chat_id)
                self.assertFalse(rec.stored_by_reference)
                # Re-opening is a no-op, not an error.
                store.close()
                store2 = Store(data_dir, owner_id=111)
                try:
                    self.assertEqual(store2.get_file(1, user_id=111).filename, "old.txt")
                finally:
                    store2.close()
            finally:
                store.close()

    def test_delete_and_clear_reference_only_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data")
            try:
                ref = store.add_file(
                    file_id="tg-1", filename="a.apk", file_type="document",
                    size=BIG, telegram_file_id="tg-1",
                    source_chat_id=111, source_message_id=9, user_id=111)
                store.add_note("keep?", user_id=111)
                # Deleting a reference-only record touches no disk file.
                deleted = store.delete_file(ref.id, user_id=111)
                self.assertEqual(deleted.id, ref.id)
                self.assertIsNone(store.get_file(ref.id, user_id=111))
                store.add_file(
                    file_id="tg-2", filename="b.pdf", file_type="document",
                    size=10, telegram_file_id="tg-2",
                    source_chat_id=111, source_message_id=10, user_id=111)
                n_files, n_notes = store.clear_all(user_id=111)
                self.assertEqual((n_files, n_notes), (1, 1))
                self.assertEqual(store.list_files(limit=10, user_id=111)[1], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
