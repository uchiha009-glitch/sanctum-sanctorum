"""Offline tests for per-user file numbering — no real Telegram calls.

Run from the project root:
    python -m unittest discover -s tests -v

The vault used to number every file with one global auto-increment id,
so the owner saw #3-#7 and a new friend's first file was #8. Files are
now numbered PER USER: each user's vault numbers its own files #1, #2,
#3... The global id stays as the database primary key but is never
shown to a user and never accepted as a lookup.

Covers:
  (a) two users each get their own #1, and numbering is independent,
  (b) /get (Store.get_file) uses the per-user number — one user's #1
      never resolves to the other user's file, and a global id is not
      a valid lookup,
  (c) resolve() by number is per-user,
  (d) delete by per-user number only deletes that user's file,
  (e) a user_num is never reused after a delete (max + 1),
  (f) /list display shows the per-user numbers,
  (g) the save-file reply quotes the per-user number,
  (h) the migration assigns per-user numbers to existing rows in id
      order (owner's old global #3-#7 become #1-#5, a friend's #8
      becomes #1), and is idempotent,
  (i) pre-multi-user rows (NULL user_id) are assigned to the owner
      first, then numbered as the owner's.
"""

from __future__ import annotations

import asyncio
import sqlite3
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


def _add(store, user_id, filename, file_id="tg-x"):
    return store.add_file(
        file_id=file_id, filename=filename, file_type="document",
        size=10, telegram_file_id=file_id, user_id=user_id,
    )


class PerUserNumberingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_two_users_each_get_their_own_number_one(self):
        a1 = _add(self.store, OWNER, "a-one.txt", "tg-a1")
        a2 = _add(self.store, OWNER, "a-two.txt", "tg-a2")
        b1 = _add(self.store, FRIEND, "b-one.txt", "tg-b1")
        # Per-user numbers: independent sequences starting at 1.
        self.assertEqual((a1.user_num, a2.user_num), (1, 2))
        self.assertEqual(b1.user_num, 1)
        # The global ids still differ (they are the database keys).
        self.assertNotEqual(a1.id, b1.id)
        self.assertEqual(a1.id, 1)
        self.assertEqual(b1.id, 3)

    def test_display_shows_per_user_number_not_global_id(self):
        a1 = _add(self.store, OWNER, "a-one.txt", "tg-a1")
        b1 = _add(self.store, FRIEND, "b-one.txt", "tg-b1")
        self.assertTrue(a1.display().startswith("#1 a-one.txt"))
        # The friend's file has global id 2 but displays as #1.
        self.assertTrue(b1.display().startswith("#1 b-one.txt"))
        self.assertNotIn("#2", b1.display())

    def test_get_uses_per_user_number(self):
        a1 = _add(self.store, OWNER, "a-one.txt", "tg-a1")
        a2 = _add(self.store, OWNER, "a-two.txt", "tg-a2")
        b1 = _add(self.store, FRIEND, "b-one.txt", "tg-b1")
        # Each user's #1 is their own file.
        self.assertEqual(self.store.get_file(1, user_id=OWNER).id, a1.id)
        self.assertEqual(self.store.get_file(1, user_id=FRIEND).id, b1.id)
        self.assertEqual(self.store.get_file(2, user_id=OWNER).id, a2.id)
        # The friend has no #2.
        self.assertIsNone(self.store.get_file(2, user_id=FRIEND))
        # A global id is NOT a valid lookup: the friend's file has
        # global id 3, but /get 3 must not find it for anyone.
        self.assertIsNone(self.store.get_file(3, user_id=OWNER))
        self.assertIsNone(self.store.get_file(3, user_id=FRIEND))
        self.assertIsNone(self.store.get_file(99, user_id=OWNER))

    def test_resolve_by_number_is_per_user(self):
        _add(self.store, OWNER, "a-one.txt", "tg-a1")
        b1 = _add(self.store, FRIEND, "b-one.txt", "tg-b1")
        matches = self.store.resolve("1", user_id=FRIEND)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].id, b1.id)
        self.assertEqual(self.store.resolve("2", user_id=FRIEND), [])
        self.assertEqual(len(self.store.resolve("1", user_id=OWNER)), 1)

    def test_delete_by_per_user_number_only_deletes_that_users_file(self):
        a1 = _add(self.store, OWNER, "a-one.txt", "tg-a1")
        b1 = _add(self.store, FRIEND, "b-one.txt", "tg-b1")
        deleted = self.store.delete_file(1, user_id=FRIEND)
        self.assertEqual(deleted.id, b1.id)
        # The owner's #1 is untouched.
        self.assertIsNotNone(self.store.get_file(1, user_id=OWNER))
        self.assertEqual(self.store.get_file(1, user_id=OWNER).id, a1.id)
        # Deleting a number the user does not have is a no-op.
        self.assertIsNone(self.store.delete_file(5, user_id=FRIEND))

    def test_next_number_is_max_plus_one(self):
        # The next number is max(existing user_num) + 1 for that user
        # only. Deleting the highest number frees it; deleting a lower
        # one does not change the next number.
        _add(self.store, OWNER, "a-one.txt", "tg-a1")
        _add(self.store, OWNER, "a-two.txt", "tg-a2")
        self.store.delete_file(2, user_id=OWNER)
        self.assertEqual(_add(self.store, OWNER, "a-new.txt", "tg-a3").user_num, 2)
        self.store.delete_file(1, user_id=OWNER)
        self.assertEqual(_add(self.store, OWNER, "a-new2.txt", "tg-a4").user_num, 3)

    def test_list_display_uses_per_user_numbers(self):
        _add(self.store, OWNER, "a-one.txt", "tg-a1")
        _add(self.store, OWNER, "a-two.txt", "tg-a2")
        _add(self.store, FRIEND, "b-one.txt", "tg-b1")
        records, total = self.store.list_files(limit=10, user_id=OWNER)
        self.assertEqual(total, 2)
        self.assertEqual([r.user_num for r in records], [2, 1])  # newest first
        self.assertTrue(records[0].display().startswith("#2 a-two.txt"))
        records, total = self.store.list_files(limit=10, user_id=FRIEND)
        self.assertEqual(total, 1)
        self.assertTrue(records[0].display().startswith("#1 b-one.txt"))


class SaveFileReplyTestCase(unittest.IsolatedAsyncioTestCase):
    """The 'Stored #N' reply quotes the PER-USER number."""

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

    async def test_second_users_first_file_is_number_one(self):
        # The owner stores two files first, so the global ids are 1-2.
        _add(self.store, OWNER, "owner-one.txt", "tg-o1")
        _add(self.store, OWNER, "owner-two.txt", "tg-o2")

        class FakeDoc:
            file_id = "tg-f1"
            file_name = "friend-file.txt"
            file_size = 10

        class FakeMessage:
            document = FakeDoc()
            photo = None
            video = None
            audio = None
            voice = None
            caption = ""
            chat_id = FRIEND
            message_id = 1
            date = SimpleNamespace(timestamp=lambda: 0)

            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **kwargs):
                self.replies.append(text)

        class FakeUpdate:
            def __init__(self, message, user_id):
                self.message = message
                self.effective_message = message
                self.effective_user = SimpleNamespace(id=user_id)
                self.effective_chat = SimpleNamespace(id=user_id)
                self.callback_query = None

        class FakeContext:
            args = []
            bot = None

        msg = FakeMessage()
        await bot_module.save_file(FakeUpdate(msg, FRIEND), FakeContext())
        self.assertEqual(len(msg.replies), 1)
        # The friend's FIRST file is #1 even though its global id is 3.
        self.assertIn("Stored #1 friend-file.txt", msg.replies[0])
        self.assertIn("Use /get 1 to get it back.", msg.replies[0])


# -- migration -------------------------------------------------------------
# A legacy database: the files table WITHOUT the user_num column, rows
# with the global ids the live bot had (owner #3-#7, friend #8).

LEGACY_FILES_SCHEMA = """
CREATE TABLE files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    filename    TEXT NOT NULL,
    stored_name TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL,
    file_type   TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    file_id     TEXT NOT NULL,
    caption     TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    telegram_file_id TEXT NOT NULL DEFAULT '',
    source_chat_id INTEGER,
    source_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    text       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    api_key    TEXT NOT NULL DEFAULT '',
    is_banned  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


class MigrationTestCase(unittest.TestCase):
    def _legacy_db(self, rows):
        """Build a temp data dir with a legacy vault.db; `rows` is a list
        of (id, user_id, filename). Returns the data dir path."""
        tmp = tempfile.TemporaryDirectory()
        data_dir = Path(tmp.name) / "data"
        data_dir.mkdir(parents=True)
        conn = sqlite3.connect(data_dir / "vault.db")
        conn.executescript(LEGACY_FILES_SCHEMA)
        for row_id, user_id, filename in rows:
            conn.execute(
                "INSERT INTO files (id, user_id, filename, stored_name,"
                " category, file_type, size, file_id, caption, created_at,"
                " telegram_file_id, source_chat_id, source_message_id)"
                " VALUES (?, ?, ?, '', 'documents', 'document', 10, ?, '',"
                " 1700000000, ?, NULL, NULL)",
                (row_id, user_id, filename, f"tg-{row_id}", f"tg-{row_id}"),
            )
        conn.commit()
        conn.close()
        return tmp, data_dir

    def test_migration_assigns_per_user_numbers_in_id_order(self):
        # Mirrors the live database: owner's global #3-#7, friend's #8.
        rows = [
            (3, OWNER, "reddit.txt"),
            (4, OWNER, "piko flags.json"),
            (5, OWNER, "reddit2.txt"),
            (6, OWNER, "Vibes.apk"),
            (7, OWNER, "RDR.mp3"),
            (8, FRIEND, "CapCut.apk"),
        ]
        tmp, data_dir = self._legacy_db(rows)
        try:
            store = Store(data_dir, owner_id=OWNER)
            try:
                owner_files, total = store.list_files(
                    limit=10, user_id=OWNER)
                self.assertEqual(total, 5)
                # Newest first: user_num 5 down to 1.
                self.assertEqual(
                    [r.user_num for r in owner_files], [5, 4, 3, 2, 1])
                self.assertEqual(
                    [r.filename for r in owner_files],
                    ["RDR.mp3", "Vibes.apk", "reddit2.txt",
                     "piko flags.json", "reddit.txt"],
                )
                # The owner's old global #3 is now his #1.
                self.assertEqual(
                    store.get_file(1, user_id=OWNER).filename, "reddit.txt")
                # The friend's global #8 is now their #1.
                friend_files, total = store.list_files(
                    limit=10, user_id=FRIEND)
                self.assertEqual(total, 1)
                self.assertEqual(friend_files[0].user_num, 1)
                self.assertEqual(friend_files[0].filename, "CapCut.apk")
                self.assertEqual(
                    store.get_file(1, user_id=FRIEND).filename, "CapCut.apk")
                # And the friend's #1 is not the owner's #1.
                self.assertNotEqual(
                    store.get_file(1, user_id=OWNER).id,
                    store.get_file(1, user_id=FRIEND).id,
                )
            finally:
                store.close()
            # Idempotent: reopening changes nothing.
            store = Store(data_dir, owner_id=OWNER)
            try:
                owner_files, _ = store.list_files(limit=10, user_id=OWNER)
                self.assertEqual(
                    [r.user_num for r in owner_files], [5, 4, 3, 2, 1])
                friend_files, _ = store.list_files(limit=10, user_id=FRIEND)
                self.assertEqual(friend_files[0].user_num, 1)
            finally:
                store.close()
        finally:
            tmp.cleanup()

    def test_migration_assigns_null_user_id_rows_to_owner_first(self):
        # Pre-multi-user rows (no user_id) belonged to the owner.
        rows = [
            (1, None, "old-one.txt"),
            (2, None, "old-two.txt"),
        ]
        tmp, data_dir = self._legacy_db(rows)
        try:
            store = Store(data_dir, owner_id=OWNER)
            try:
                records, total = store.list_files(limit=10, user_id=OWNER)
                self.assertEqual(total, 2)
                self.assertEqual(
                    sorted(r.user_num for r in records), [1, 2])
                self.assertEqual(
                    store.get_file(1, user_id=OWNER).filename, "old-one.txt")
            finally:
                store.close()
        finally:
            tmp.cleanup()

    def test_new_file_after_migration_continues_the_sequence(self):
        rows = [(3, OWNER, "reddit.txt"), (8, FRIEND, "CapCut.apk")]
        tmp, data_dir = self._legacy_db(rows)
        try:
            store = Store(data_dir, owner_id=OWNER)
            try:
                rec = _add(store, OWNER, "new.txt", "tg-new")
                self.assertEqual(rec.user_num, 2)
                rec = _add(store, FRIEND, "new2.txt", "tg-new2")
                self.assertEqual(rec.user_num, 2)
            finally:
                store.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
