"""Offline tests for duplicate detection, the dedupe migration, the
download retry, and the /ask joke fix — no real Telegram calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers:
  (a) saving a file whose Telegram file_id is already in the sender's
      vault stores NOTHING new and replies "Already in your vault as
      #N" (with the original number, filename, category and size),
  (b) a duplicate costs no quota and no daily-limit slot,
  (c) a duplicate is per-user: the same file_id in another user's
      vault stores normally as their own #1,
  (d) a renamed re-send (same file_id, different filename) still
      counts as a duplicate and points at the original,
  (e) the migration (runs on every Store open) removes the duplicate
      rows and renumbers the affected vault gaplessly — the live
      vault's shape (13 files, 4 duplicate pairs) becomes 9 files
      #1-#9, originals keeping their numbers — and is idempotent,
  (f) legacy on-disk rows (empty telegram_file_id) are never treated
      as duplicates, by the save check or by the migration,
  (g) /delete does NOT renumber: the number is a stable reference,
      the gap stays, and the next file takes MAX+1,
  (h) the download retry in _download_via_bot: a transient failure
      (the egress proxy drops connections) is retried and a file
      that succeeds on the 2nd attempt comes back; a file that
      always fails returns None after 3 attempts,
  (i) /ask "tell me a joke" tells a joke (plain chat always did; /ask
      used to fall through to the dry fallback),
  (j) /ask "how many files do I have?" uses the same friendly
      category names as /stats ("images", not "photos").
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Make the project root importable (bot.py, vault.py, assistant.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as bot_module
from assistant import _JOKES, answer_ask
from vault import Store

OWNER = 7301946219
FRIEND = 6428830709


class FakeMessage:
    """Stands in for telegram.Message in the save path."""

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
        self.replies = []  # reply_text calls: (text, kwargs)

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


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
        self.bot = bot


def _tg_file(file_id, file_name=None, file_size=0):
    return SimpleNamespace(file_id=file_id, file_name=file_name,
                           file_size=file_size)


def _save(store, file_id, filename, file_type, user_id, size=100,
          source_message_id=1):
    """Store a file by reference, as the live bot does."""
    return store.add_file(
        file_id=file_id, filename=filename, file_type=file_type, size=size,
        telegram_file_id=file_id, source_chat_id=user_id,
        source_message_id=source_message_id, user_id=user_id)


class FindDuplicateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_find_returns_existing_record(self):
        rec = _save(self.store, "tg-rdr", "RDR.mp3", "audio", 111)
        found = self.store.find_by_telegram_file_id("tg-rdr", user_id=111)
        self.assertIsNotNone(found)
        self.assertEqual(found.user_num, rec.user_num)
        self.assertEqual(found.filename, "RDR.mp3")

    def test_find_returns_none_for_unknown_file_id(self):
        _save(self.store, "tg-rdr", "RDR.mp3", "audio", 111)
        self.assertIsNone(
            self.store.find_by_telegram_file_id("tg-other", user_id=111))

    def test_find_returns_none_for_empty_file_id(self):
        # Legacy on-disk records have no file_id; two of them must
        # never count as duplicates of each other.
        self.store.add_file(file_id="d1", filename="old.txt",
                            file_type="document", data=b"x", user_id=111)
        self.store.add_file(file_id="d2", filename="old2.txt",
                            file_type="document", data=b"y", user_id=111)
        self.assertIsNone(
            self.store.find_by_telegram_file_id("", user_id=111))

    def test_find_is_per_user(self):
        _save(self.store, "tg-rdr", "RDR.mp3", "audio", 111)
        self.assertIsNone(
            self.store.find_by_telegram_file_id("tg-rdr", user_id=222))


class SaveDuplicateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self._old_store = bot_module.store
        bot_module.store = lambda: self.store

    def tearDown(self):
        bot_module.store = self._old_store
        self.store.close()
        self.tmp.cleanup()

    def _send(self, file_id, filename, user_id=111, message_id=1):
        msg = FakeMessage(
            document=_tg_file(file_id, filename, 659500),
            chat_id=user_id, message_id=message_id)
        update = FakeUpdate(msg, user_id=user_id)
        asyncio.run(bot_module.save_file(update, FakeContext()))
        return msg

    def test_resending_same_file_returns_original_number(self):
        first = self._send("tg-rdr", "RDR.mp3", message_id=1)
        self.assertIn("Stored #1 RDR.mp3", first.replies[0][0])
        second = self._send("tg-rdr", "RDR.mp3", message_id=2)
        self.assertIn("Already in your vault as #1 RDR.mp3",
                      second.replies[0][0])
        self.assertIn("Use /get 1", second.replies[0][0])
        _, total = self.store.list_files(limit=10, user_id=111)
        self.assertEqual(total, 1)

    def test_duplicate_costs_no_quota_or_daily_slot(self):
        self._send("tg-a", "a.txt", message_id=1)
        self._send("tg-a", "a.txt", message_id=2)  # duplicate, free
        third = self._send("tg-b", "b.txt", message_id=3)
        # The next NEW file is still #2: the duplicate took no slot.
        self.assertIn("Stored #2 b.txt", third.replies[0][0])
        _, total = self.store.list_files(limit=10, user_id=111)
        self.assertEqual(total, 2)

    def test_same_file_other_user_is_not_duplicate(self):
        self._send("tg-rdr", "RDR.mp3", user_id=111, message_id=1)
        friend = self._send("tg-rdr", "RDR.mp3", user_id=222, message_id=1)
        self.assertIn("Stored #1 RDR.mp3", friend.replies[0][0])
        _, total = self.store.list_files(limit=10, user_id=222)
        self.assertEqual(total, 1)

    def test_renamed_resend_still_duplicate(self):
        self._send("tg-rdr", "RDR.mp3", message_id=1)
        second = self._send("tg-rdr", "RDR (1).mp3", message_id=2)
        # Points at the ORIGINAL name and number, not the new name.
        self.assertIn("Already in your vault as #1 RDR.mp3",
                      second.replies[0][0])
        _, total = self.store.list_files(limit=10, user_id=111)
        self.assertEqual(total, 1)


class MigrationDedupeTestCase(unittest.TestCase):
    """Seed a vault with duplicates, close, reopen (migration runs),
    and check the result — the live vault's shape, 13 files with 4
    duplicate pairs, must become 9 files numbered #1-#9."""

    # (filename, file_type, telegram file_id) — the live vault's shape.
    LIVE_SHAPE = [
        ("video_20261115_132524.mp4", "video", "tg-video"),
        ("SUJATHA SIMHADRI.zip", "document", "tg-zip"),
        ("RDR.mp3", "audio", "tg-rdr"),
        ("photo_20261115_132634.jpg", "photo", "tg-photo1"),
        ("Vibes.apk", "document", "tg-vibes"),
        ("reddit.txt", "document", "tg-reddit"),
        ("photo_20261115_134646.jpg", "photo", "tg-photo2"),
        ("RDR.mp3", "audio", "tg-rdr"),                       # dup of #3
        ("reddit.txt", "document", "tg-reddit"),               # dup of #6
        ("piko flags.json", "document", "tg-piko"),
        ("SUJATHA SIMHADRI.zip", "document", "tg-zip"),       # dup of #2
        ("photo_20261115_135956.jpg", "photo", "tg-photo3"),
        ("photo_20261115_140006.jpg", "photo", "tg-photo3"),   # dup of #12
    ]

    def _seed(self, rows, user_id=OWNER):
        tmp = tempfile.TemporaryDirectory()
        data_dir = Path(tmp.name) / "data"
        store = Store(data_dir)
        for i, (name, kind, file_id) in enumerate(rows, start=1):
            _save(store, file_id, name, kind, user_id,
                  source_message_id=i)
        store.close()
        return tmp, data_dir

    def test_migration_removes_dupes_and_renumbers(self):
        tmp, data_dir = self._seed(self.LIVE_SHAPE)
        try:
            store = Store(data_dir)
            try:
                files, total = store.list_files(limit=20, user_id=OWNER)
                self.assertEqual(total, 9)
                numbers = sorted(f.user_num for f in files)
                self.assertEqual(numbers, list(range(1, 10)))
                by_num = {f.user_num: f.filename for f in files}
                # Originals keep their numbers; files after a removed
                # duplicate shift down (#10 piko -> #8, #12 photo -> #9).
                self.assertEqual(by_num[1], "video_20261115_132524.mp4")
                self.assertEqual(by_num[2], "SUJATHA SIMHADRI.zip")
                self.assertEqual(by_num[3], "RDR.mp3")
                self.assertEqual(by_num[6], "reddit.txt")
                self.assertEqual(by_num[8], "piko flags.json")
                self.assertEqual(by_num[9], "photo_20261115_135956.jpg")
                # No file_id appears twice for this user any more.
                ids = [f.telegram_file_id for f in files]
                self.assertEqual(len(ids), len(set(ids)))
            finally:
                store.close()
            # Idempotent: reopening changes nothing.
            store = Store(data_dir)
            try:
                _, total = store.list_files(limit=20, user_id=OWNER)
                self.assertEqual(total, 9)
            finally:
                store.close()
        finally:
            tmp.cleanup()

    def test_migration_is_per_user(self):
        rows = [
            ("a.txt", "document", "tg-a"),
            ("a.txt", "document", "tg-a"),  # owner's duplicate
            ("b.txt", "document", "tg-b"),
        ]
        tmp, data_dir = self._seed(rows, user_id=OWNER)
        try:
            # The friend has the same file_id — that is NOT a duplicate.
            store = Store(data_dir)
            _save(store, "tg-a", "a.txt", "document", FRIEND)
            store.close()
            store = Store(data_dir)
            try:
                _, owner_total = store.list_files(limit=10, user_id=OWNER)
                _, friend_total = store.list_files(limit=10, user_id=FRIEND)
                self.assertEqual(owner_total, 2)
                self.assertEqual(friend_total, 1)
            finally:
                store.close()
        finally:
            tmp.cleanup()

    def test_migration_leaves_empty_file_ids_alone(self):
        tmp = tempfile.TemporaryDirectory()
        data_dir = Path(tmp.name) / "data"
        store = Store(data_dir)
        store.add_file(file_id="d1", filename="old.txt",
                       file_type="document", data=b"x", user_id=OWNER)
        store.add_file(file_id="d2", filename="old2.txt",
                       file_type="document", data=b"y", user_id=OWNER)
        store.close()
        try:
            store = Store(data_dir)
            try:
                _, total = store.list_files(limit=10, user_id=OWNER)
                self.assertEqual(total, 2)
            finally:
                store.close()
        finally:
            tmp.cleanup()

    def test_dedupe_files_reports_what_it_did(self):
        tmp, data_dir = self._seed(self.LIVE_SHAPE)
        try:
            store = Store(data_dir)
            try:
                # Already deduped by the open above: a second call is a
                # no-op.
                result = store.dedupe_files()
                self.assertEqual(result, {"removed": 0, "users": []})
            finally:
                store.close()
        finally:
            tmp.cleanup()


class DeleteGapTestCase(unittest.TestCase):
    """Numbers are stable references: deleting #2 leaves a gap, and
    the next file takes MAX+1 (#4) — it does NOT renumber to fill
    the gap. This is deliberate: /get N and "already in your vault
    as #N" must keep meaning the same file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_delete_leaves_gap_and_next_is_max_plus_one(self):
        _save(self.store, "tg-1", "one.txt", "document", 111)
        _save(self.store, "tg-2", "two.txt", "document", 111)
        _save(self.store, "tg-3", "three.txt", "document", 111)
        deleted = self.store.delete_file(2, user_id=111)
        self.assertEqual(deleted.filename, "two.txt")
        files, total = self.store.list_files(limit=10, user_id=111)
        self.assertEqual(total, 2)
        self.assertEqual(sorted(f.user_num for f in files), [1, 3])
        rec = _save(self.store, "tg-4", "four.txt", "document", 111)
        self.assertEqual(rec.user_num, 4)
        files, total = self.store.list_files(limit=10, user_id=111)
        self.assertEqual(sorted(f.user_num for f in files), [1, 3, 4])


class FlakyBot:
    """A bot whose get_file fails `fail_times` times, then works."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls: list[str] = []

    async def get_file(self, file_id):
        self.calls.append(file_id)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("connection dropped by the proxy")
        tg_file = SimpleNamespace(
            download_as_bytearray=AsyncMock(
                return_value=bytearray(b"hello")))
        return tg_file


class DownloadRetryTestCase(unittest.TestCase):
    def setUp(self):
        self._old_delays = bot_module._DOWNLOAD_RETRY_DELAYS
        bot_module._DOWNLOAD_RETRY_DELAYS = (0, 0)

    def tearDown(self):
        bot_module._DOWNLOAD_RETRY_DELAYS = self._old_delays

    def test_fail_once_then_succeeds(self):
        bot = FlakyBot(fail_times=1)
        data = asyncio.run(
            bot_module._download_via_bot(FakeContext(bot=bot), "tg-x"))
        self.assertEqual(data, b"hello")
        self.assertEqual(len(bot.calls), 2)

    def test_fail_always_returns_none_after_three_attempts(self):
        bot = FlakyBot(fail_times=99)
        data = asyncio.run(
            bot_module._download_via_bot(FakeContext(bot=bot), "tg-x"))
        self.assertIsNone(data)
        self.assertEqual(len(bot.calls), 3)


class AskJokeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        # Reset the joke rotation so the test is deterministic.
        import assistant as assistant_module
        assistant_module._JOKE_INDEX = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ask_tell_me_a_joke_returns_a_joke(self):
        out = answer_ask("tell me a joke", self.store, 111)
        self.assertIn(out, _JOKES)

    def test_ask_jokes_rotate(self):
        first = answer_ask("tell me a joke", self.store, 111)
        second = answer_ask("tell me a joke", self.store, 111)
        self.assertNotEqual(first, second)

    def test_ask_unrelated_still_falls_back(self):
        out = answer_ask("tell me about quantum physics", self.store, 111)
        self.assertIn("beyond my local wards", out)

    def test_ask_how_many_files_uses_stats_labels(self):
        _save(self.store, "tg-pic", "pic.jpg", "photo", 111)
        _save(self.store, "tg-apk", "Vibes.apk", "document", 111)
        out = answer_ask("how many files do I have?", self.store, 111)
        self.assertIn("images:", out)
        self.assertNotIn("photos:", out)
        self.assertIn("apps (APKs & archives):", out)


if __name__ == "__main__":
    unittest.main()
