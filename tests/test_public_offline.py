"""Offline tests for the PUBLIC multi-user Sanctum bot — no real
Telegram calls, no real Gemini calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers:
  (a) isolation: two users cannot see each other's files/notes — list,
      get, search, delete, stats, clear are all scoped; asking for
      another user's file id is simply "not found",
  (b) the migration assigns pre-existing rows to the owner id,
  (c) /chat is the keeper's alone (Nov 15, 2026): the owner's key for
      the owner; any other user is refused with no AI call, no key
      check, no BYOK prompt — even one who linked their own key,
  (d) /setkey stores a key (encrypted at rest), /delkey removes it,
      /mykey never reveals the key value,
  (e) per-user file limits are enforced (count, total size, per-day),
  (f) /clear only clears the requesting user's vault,
  (g) /ban blocks a banned user on every command; /unban restores them,
  (h) /admin shows aggregate numbers only — no names, no contents.
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

import ai as ai_module
import bot as bot_module
import config
import vault as vault_module
from keystore import KeyVault
from vault import Store

OWNER = 111
USER_A = 222
USER_B = 333


class FakeMessage:
    def __init__(self, text="", document=None, photo=None, video=None,
                 audio=None, voice=None, caption="", chat_id=111,
                 message_id=1, date=None):
        self.text = text
        self.caption = caption
        self.document = document
        self.photo = photo
        self.video = video
        self.audio = audio
        self.voice = voice
        self.chat_id = chat_id
        self.message_id = message_id
        self.date = date or datetime.now(timezone.utc)
        self.replies = []
        self.sent_files = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)

    async def _record_send(self, kind, **kwargs):
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


class FakeBot:
    def __init__(self, forward_fails=False):
        self.forwarded = []
        self.forward_fails = forward_fails

    async def forward_message(self, chat_id, from_chat_id, message_id):
        if self.forward_fails:
            raise RuntimeError("message to forward not found")
        self.forwarded.append((chat_id, from_chat_id, message_id))
        return SimpleNamespace(message_id=999)


class FakeUpdate:
    def __init__(self, message=None, user_id=OWNER, callback_query=None,
                 text=""):
        self.message = message if message is not None else FakeMessage(text)
        self.effective_message = self.message
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=user_id)
        self.callback_query = callback_query


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=None)
        self.bot = bot or FakeBot()


def _tg_file(file_id, file_name=None, file_size=0):
    return SimpleNamespace(file_id=file_id, file_name=file_name,
                           file_size=file_size)


class PublicStoreTestCase(unittest.TestCase):
    """(a) Store-level isolation between two users."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data", owner_id=OWNER)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed_two_users(self):
        a1 = self.store.add_file(
            file_id="a1", filename="alice.pdf", file_type="document",
            size=100, telegram_file_id="a1", source_chat_id=USER_A,
            source_message_id=1, user_id=USER_A,
        )
        a2 = self.store.add_file(
            file_id="a2", filename="alice.jpg", file_type="photo",
            size=200, telegram_file_id="a2", source_chat_id=USER_A,
            source_message_id=2, user_id=USER_A,
        )
        b1 = self.store.add_file(
            file_id="b1", filename="bob.pdf", file_type="document",
            size=300, telegram_file_id="b1", source_chat_id=USER_B,
            source_message_id=1, user_id=USER_B,
        )
        na = self.store.add_note("alice secret note", user_id=USER_A)
        nb = self.store.add_note("bob secret note", user_id=USER_B)
        return a1, a2, b1, na, nb

    def test_list_is_scoped(self):
        a1, a2, b1, _, _ = self._seed_two_users()
        records, total = self.store.list_files(limit=10, user_id=USER_A)
        self.assertEqual(total, 2)
        self.assertEqual({r.id for r in records}, {a1.id, a2.id})
        records, total = self.store.list_files(limit=10, user_id=USER_B)
        self.assertEqual(total, 1)
        self.assertEqual(records[0].id, b1.id)

    def test_get_other_users_file_is_not_found(self):
        a1, _, b1, _, _ = self._seed_two_users()
        # Numbers are per-user: each user's #1 is their OWN file.
        self.assertEqual(self.store.get_file(1, user_id=USER_A).id, a1.id)
        self.assertEqual(self.store.get_file(1, user_id=USER_B).id, b1.id)
        # A number only the other user has is simply "not found" — the
        # other user's file is never revealed.
        self.assertIsNone(self.store.get_file(2, user_id=USER_B))
        self.assertIsNotNone(self.store.get_file(2, user_id=USER_A))

    def test_search_and_resolve_are_scoped(self):
        self._seed_two_users()
        self.assertEqual(len(self.store.search_files("alice", user_id=USER_A)), 2)
        self.assertEqual(self.store.search_files("alice", user_id=USER_B), [])
        self.assertEqual(self.store.search_files("bob", user_id=USER_A), [])
        self.assertEqual(len(self.store.resolve("alice", user_id=USER_A)), 2)
        self.assertEqual(self.store.resolve("alice", user_id=USER_B), [])
        # Numeric resolve of a number only the other user has is also
        # "not found" — and "1" for USER_B is USER_B's own file, not
        # USER_A's.
        self.assertEqual(self.store.resolve("2", user_id=USER_B), [])
        self.assertEqual(
            self.store.resolve("1", user_id=USER_B)[0].filename, "bob.pdf")

    def test_delete_other_users_file_does_nothing(self):
        a1, a2, b1, _, _ = self._seed_two_users()
        # USER_B has only #1, so deleting #2 (a number only USER_A has)
        # does nothing, and USER_A's files survive.
        self.assertIsNone(self.store.delete_file(2, user_id=USER_B))
        self.assertIsNotNone(self.store.get_file(2, user_id=USER_A))
        self.assertEqual(self.store.get_file(2, user_id=USER_A).id, a2.id)
        # Deleting one's OWN #1 works.
        self.assertIsNotNone(self.store.delete_file(1, user_id=USER_B))
        self.assertIsNone(self.store.get_file(1, user_id=USER_B))
        self.assertIsNotNone(self.store.get_file(1, user_id=USER_A))

    def test_stats_are_scoped(self):
        self._seed_two_users()
        s = self.store.stats(user_id=USER_A)
        self.assertEqual((s["total_files"], s["total_size"]), (2, 300))
        s = self.store.stats(user_id=USER_B)
        self.assertEqual((s["total_files"], s["total_size"]), (1, 300))

    def test_notes_are_scoped(self):
        _, _, _, na, nb = self._seed_two_users()
        notes, total = self.store.list_notes(user_id=USER_A)
        self.assertEqual(total, 1)
        self.assertEqual(notes[0].id, na.id)
        self.assertIsNone(self.store.get_note(na.id, user_id=USER_B))
        self.assertEqual(self.store.search_notes("bob", user_id=USER_A), [])
        self.assertEqual(len(self.store.search_notes("bob", user_id=USER_B)), 1)
        self.assertIsNone(self.store.delete_note(nb.id, user_id=USER_A))
        self.assertIsNotNone(self.store.delete_note(nb.id, user_id=USER_B))

    def test_clear_only_clears_requesting_user(self):
        self._seed_two_users()
        n_files, n_notes = self.store.clear_all(user_id=USER_B)
        self.assertEqual((n_files, n_notes), (1, 1))
        self.assertEqual(self.store.list_files(limit=10, user_id=USER_A)[1], 2)
        self.assertEqual(self.store.list_notes(user_id=USER_A)[1], 1)
        self.assertEqual(self.store.list_files(limit=10, user_id=USER_B)[1], 0)


class MigrationTestCase(unittest.TestCase):
    """(b) Pre-existing rows (no user_id) are assigned to the owner."""

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

    def test_legacy_rows_assigned_to_owner(self):
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
            conn.execute(
                "INSERT INTO notes (text, created_at)"
                " VALUES ('old note', 1763200000)"
            )
            conn.commit()
            conn.close()

            store = Store(data_dir, owner_id=OWNER)
            try:
                rec = store.get_file(1, user_id=OWNER)
                self.assertIsNotNone(rec)
                self.assertEqual(rec.user_id, OWNER)
                # Nobody else can see it.
                self.assertIsNone(store.get_file(1, user_id=USER_A))
                note = store.get_note(1, user_id=OWNER)
                self.assertIsNotNone(note)
                self.assertEqual(note.user_id, OWNER)
                # Idempotent: re-opening changes nothing.
                store.close()
                store2 = Store(data_dir, owner_id=OWNER)
                try:
                    self.assertEqual(
                        store2.get_file(1, user_id=OWNER).filename, "old.txt"
                    )
                finally:
                    store2.close()
            finally:
                store.close()

    def test_mixed_rows_only_nulls_are_assigned(self):
        """A database that already has user_ids keeps them; only NULL
        rows are assigned to the owner."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data", owner_id=OWNER)
            try:
                store.add_file(file_id="a", filename="mine.pdf",
                               file_type="document", size=1, user_id=USER_A)
                # Simulate a pre-multi-user row with no user_id.
                store._conn.execute(
                    "INSERT INTO files (user_id, filename, stored_name,"
                    " category, file_type, size, file_id, caption, created_at)"
                    " VALUES (NULL, 'legacy.pdf', '', 'documents',"
                    " 'document', 1, 'tg-legacy', '', 1763200000)"
                )
                store._conn.commit()
                store.close()
                store2 = Store(Path(tmp) / "data", owner_id=OWNER)
                try:
                    # The legacy row belonged to the owner and is now
                    # the owner's #1 (per-user numbering starts at 1).
                    legacy = store2.get_file(1, user_id=OWNER)
                    self.assertIsNotNone(legacy)
                    self.assertEqual(legacy.filename, "legacy.pdf")
                    # The other user's row was NOT reassigned, and is
                    # still their #1.
                    self.assertIsNotNone(
                        store2.get_file(1, user_id=USER_A)
                    )
                    # Neither user has a #2.
                    self.assertIsNone(store2.get_file(2, user_id=OWNER))
                    self.assertIsNone(store2.get_file(2, user_id=USER_A))
                finally:
                    store2.close()
            finally:
                store.close()


class KeyStorageTestCase(unittest.TestCase):
    """(d) Keys are encrypted at rest; (d) mykey never reveals the value."""

    def test_key_encrypted_at_rest_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data", owner_id=OWNER)
            try:
                secret = "AIzaSyD-USER-KEY-0123456789abcdefghij"
                store.set_user_key(USER_A, secret)
                # The raw value in the database is NOT the key.
                row = store._conn.execute(
                    "SELECT api_key FROM users WHERE user_id = ?",
                    (USER_A,),
                ).fetchone()
                self.assertTrue(row[0])
                self.assertNotEqual(row[0], secret)
                self.assertNotIn("AIzaSyD", row[0])
                # It decrypts back to exactly the key.
                self.assertEqual(store.get_user_key(USER_A), secret)
                # A vault with a different secret cannot read it.
                other = KeyVault(Path(tmp) / "other")
                self.assertIsNone(other.decrypt(row[0]))
            finally:
                store.close()

    def test_delete_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data", owner_id=OWNER)
            try:
                self.assertFalse(store.delete_user_key(USER_A))
                store.set_user_key(USER_A, "AIzaSyD-USER-KEY-0123456789abcdefghij")
                self.assertTrue(store.delete_user_key(USER_A))
                self.assertIsNone(store.get_user_key(USER_A))
                self.assertFalse(store.delete_user_key(USER_A))
            finally:
                store.close()


class LimitsTestCase(unittest.TestCase):
    """(e) Per-user limits: file count, total size, files per day."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data", owner_id=OWNER)
        self._old = (vault_module.MAX_FILES_PER_USER,
                     vault_module.MAX_BYTES_PER_USER,
                     vault_module.MAX_FILES_PER_DAY)

    def tearDown(self):
        (vault_module.MAX_FILES_PER_USER, vault_module.MAX_BYTES_PER_USER,
         vault_module.MAX_FILES_PER_DAY) = self._old
        self.store.close()
        self.tmp.cleanup()

    def test_file_count_limit(self):
        vault_module.MAX_FILES_PER_USER = 2
        self.store.add_file(file_id="f0", filename="f0.txt",
                            file_type="document", size=10, user_id=USER_A)
        self.assertIsNone(self.store.quota_check(USER_A, 10))  # 1 of 2: room
        self.store.add_file(file_id="f1", filename="f1.txt",
                            file_type="document", size=10, user_id=USER_A)
        err = self.store.quota_check(USER_A, 10)  # 2 of 2: full
        self.assertIsNotNone(err)
        self.assertIn("2 files", err)
        # Another user is unaffected.
        self.assertIsNone(self.store.quota_check(USER_B, 10))

    def test_total_size_limit(self):
        vault_module.MAX_BYTES_PER_USER = 1000
        self.store.add_file(file_id="big", filename="big.bin",
                            file_type="document", size=900, user_id=USER_A)
        self.assertIsNone(self.store.quota_check(USER_A, 100))
        err = self.store.quota_check(USER_A, 101)
        self.assertIsNotNone(err)
        self.assertIn("limit", err)

    def test_daily_limit(self):
        vault_module.MAX_FILES_PER_DAY = 2
        for i in range(2):
            self.store.add_file(file_id=f"d{i}", filename=f"d{i}.txt",
                                file_type="document", size=1, user_id=USER_A)
        err = self.store.quota_check(USER_A, 1)
        self.assertIsNotNone(err)
        self.assertIn("2 files", err)
        self.assertIn("day", err)


class PublicHandlerTestCase(unittest.IsolatedAsyncioTestCase):
    """Handler-level tests: isolation, BYOK routing, keys, ban, admin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data", owner_id=OWNER)
        self.old_token, self.old_owner, self.old_key = (
            config.BOT_TOKEN, config.OWNER_ID, config.GEMINI_API_KEY,
        )
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = OWNER
        config.GEMINI_API_KEY = None
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

    def _seed(self, user_id, filename="doc.pdf", file_id="tg-1",
              file_type="document", size=1000):
        return self.store.add_file(
            file_id=file_id, filename=filename, file_type=file_type,
            size=size, telegram_file_id=file_id,
            source_chat_id=user_id, source_message_id=10, user_id=user_id,
        )

    # -- (a) handler-level isolation --------------------------------------

    async def test_get_search_list_stats_scoped_at_handler_level(self):
        self._seed(USER_A, filename="alice.pdf")
        self._seed(USER_B, filename="bob.pdf")
        # USER_B lists: only bob.pdf.
        msg = FakeMessage()
        await bot_module.list_cmd(FakeUpdate(msg, user_id=USER_B),
                                  FakeContext())
        self.assertIn("bob.pdf", msg.replies[0])
        self.assertNotIn("alice.pdf", msg.replies[0])
        # USER_B searches for alice: nothing.
        msg2 = FakeMessage()
        await bot_module.search_cmd(FakeUpdate(msg2, user_id=USER_B),
                                    FakeContext(args=["alice"]))
        self.assertIn("Nothing matches", msg2.replies[0])
        # USER_B stats: only their own file.
        msg3 = FakeMessage()
        await bot_module.stats_cmd(FakeUpdate(msg3, user_id=USER_B),
                                   FakeContext())
        self.assertIn("Files: 1", msg3.replies[0])
        # USER_B deletes a number only USER_A has: not found, and
        # USER_A's file survives. (USER_B's own #1 is bob.pdf — deleting
        # "1" would delete their own file, not alice's.)
        msg4 = FakeMessage()
        await bot_module.delete_cmd(FakeUpdate(msg4, user_id=USER_B),
                                    FakeContext(args=["2"]))
        self.assertIn("No file matches", msg4.replies[0])
        self.assertIsNotNone(self.store.get_file(1, user_id=USER_A))

    async def test_clear_handler_only_clears_requesting_user(self):
        self._seed(USER_A, filename="alice.pdf")
        self._seed(USER_B, filename="bob.pdf")
        self.store.add_note("alice note", user_id=USER_A)
        self.store.add_note("bob note", user_id=USER_B)
        msg = FakeMessage()
        await bot_module.clear_cmd(FakeUpdate(msg, user_id=USER_B),
                                   FakeContext())
        self.assertIn("1 file(s)", msg.replies[0])
        cb = FakeCallbackQuery("clear:yes", user_id=USER_B)
        await bot_module.clear_confirm(
            FakeUpdate(callback_query=cb, user_id=USER_B), FakeContext()
        )
        self.assertIn("Vault wiped", cb.edits[0])
        self.assertEqual(self.store.list_files(limit=10, user_id=USER_A)[1], 1)
        self.assertEqual(self.store.list_notes(user_id=USER_A)[1], 1)
        self.assertEqual(self.store.list_files(limit=10, user_id=USER_B)[1], 0)
        self.assertEqual(self.store.list_notes(user_id=USER_B)[1], 0)

    async def test_save_enforces_daily_limit(self):
        vault_module.MAX_FILES_PER_DAY = 1
        old = vault_module.MAX_FILES_PER_DAY
        try:
            doc = _tg_file("tg-1", "one.pdf", 10)
            msg = FakeMessage(document=doc, chat_id=USER_A, message_id=1)
            await bot_module.save_file(
                FakeUpdate(msg, user_id=USER_A), FakeContext(bot=FakeBot())
            )
            self.assertIn("Stored #1", msg.replies[0])
            doc2 = _tg_file("tg-2", "two.pdf", 10)
            msg2 = FakeMessage(document=doc2, chat_id=USER_A, message_id=2)
            await bot_module.save_file(
                FakeUpdate(msg2, user_id=USER_A), FakeContext(bot=FakeBot())
            )
            self.assertIn("daily limit", msg2.replies[0])
            self.assertEqual(
                self.store.list_files(limit=10, user_id=USER_A)[1], 1
            )
        finally:
            vault_module.MAX_FILES_PER_DAY = old

    # -- (c) /chat routing -------------------------------------------------

    def _mock_ai(self, calls):
        async def _fake(api_key, model_name, history, message):
            calls.append(api_key)
            return "Mocked AI reply."
        ai_module._generate_rest = _fake

    async def test_chat_owner_uses_owner_key(self):
        config.GEMINI_API_KEY = "owner-key-for-test"
        calls = []
        self._mock_ai(calls)
        update = FakeUpdate(user_id=OWNER)
        await bot_module.chat_cmd(update, FakeContext(args=["hello"]))
        self.assertEqual(update.message.replies, ["Mocked AI reply."])
        self.assertEqual(calls, ["owner-key-for-test"])

    async def test_chat_non_owner_with_linked_key_refused(self):
        # /chat is the keeper's alone: a linked key does not open it.
        config.GEMINI_API_KEY = "owner-key-for-test"
        self.store.set_user_key(USER_A, "user-key-for-test-0123456789")
        calls = []
        self._mock_ai(calls)
        update = FakeUpdate(user_id=USER_A)
        await bot_module.chat_cmd(update, FakeContext(args=["hello"]))
        self.assertIn("keeper's counsel", update.message.replies[0])
        self.assertNotIn("/setkey", update.message.replies[0])
        self.assertEqual(calls, [])

    async def test_chat_non_owner_without_key_refused(self):
        # /chat is the keeper's alone: no BYOK prompt, just the refusal.
        config.GEMINI_API_KEY = "owner-key-for-test"

        async def _boom(*a, **k):
            raise AssertionError("Gemini must not be called here")

        ai_module._generate_rest = _boom
        update = FakeUpdate(user_id=USER_A)
        await bot_module.chat_cmd(update, FakeContext(args=["hello"]))
        reply = update.message.replies[0]
        self.assertIn("keeper's counsel", reply)
        self.assertNotIn("/setkey", reply)
        self.assertNotIn("owner-key-for-test", reply)

    async def test_chat_non_owner_refused_even_without_owner_key(self):
        # Refusal does not depend on the owner having a key at all.
        config.GEMINI_API_KEY = None
        self.store.set_user_key(USER_A, "user-key-for-test-0123456789")
        calls = []
        self._mock_ai(calls)
        update = FakeUpdate(user_id=USER_A)
        await bot_module.chat_cmd(update, FakeContext(args=["hello"]))
        self.assertIn("keeper's counsel", update.message.replies[0])
        self.assertEqual(calls, [])

    # -- (d) /setkey /delkey /mykey ----------------------------------------

    async def test_setkey_delkey_mykey_round_trip(self):
        key = "AIzaSyD-USER-KEY-0123456789abcdefghij"
        # /mykey before linking: not linked, and no key shown.
        msg = FakeMessage()
        await bot_module.mykey_cmd(FakeUpdate(msg, user_id=USER_A),
                                   FakeContext())
        self.assertIn("not linked", msg.replies[0])
        self.assertNotIn(key, msg.replies[0])
        # /setkey links it; the reply never echoes the key.
        msg2 = FakeMessage()
        await bot_module.setkey_cmd(FakeUpdate(msg2, user_id=USER_A),
                                    FakeContext(args=[key]))
        self.assertIn("linked", msg2.replies[0])
        self.assertNotIn(key, msg2.replies[0])
        self.assertEqual(self.store.get_user_key(USER_A), key)
        # /mykey now: linked, still no key value shown.
        msg3 = FakeMessage()
        await bot_module.mykey_cmd(FakeUpdate(msg3, user_id=USER_A),
                                   FakeContext())
        self.assertIn("linked", msg3.replies[0])
        self.assertNotIn("not linked", msg3.replies[0])
        self.assertNotIn(key, msg3.replies[0])
        self.assertNotIn("AIza", msg3.replies[0])
        # /delkey removes it.
        msg4 = FakeMessage()
        await bot_module.delkey_cmd(FakeUpdate(msg4, user_id=USER_A),
                                    FakeContext())
        self.assertIn("removed", msg4.replies[0])
        self.assertIsNone(self.store.get_user_key(USER_A))
        msg5 = FakeMessage()
        await bot_module.mykey_cmd(FakeUpdate(msg5, user_id=USER_A),
                                   FakeContext())
        self.assertIn("not linked", msg5.replies[0])

    async def test_setkey_rejects_garbage(self):
        msg = FakeMessage()
        await bot_module.setkey_cmd(FakeUpdate(msg, user_id=USER_A),
                                    FakeContext(args=["short"]))
        self.assertIn("doesn't look like", msg.replies[0])
        self.assertIsNone(self.store.get_user_key(USER_A))
        msg2 = FakeMessage()
        await bot_module.setkey_cmd(FakeUpdate(msg2, user_id=USER_A),
                                    FakeContext(args=[]))
        self.assertIn("doesn't look like", msg2.replies[0])

    # -- (g) ban / unban ----------------------------------------------------

    async def test_ban_blocks_every_command_and_unban_restores(self):
        self._seed(USER_A, filename="alice.pdf")
        # Owner bans USER_A.
        msg = FakeMessage()
        await bot_module.ban_cmd(FakeUpdate(msg, user_id=OWNER),
                                 FakeContext(args=[str(USER_A)]))
        self.assertIn("banned", msg.replies[0])
        # Every command is refused now.
        for handler, ctx in (
            (bot_module.list_cmd, FakeContext()),
            (bot_module.stats_cmd, FakeContext()),
            (bot_module.chat_cmd, FakeContext(args=["hi"])),
            (bot_module.help_cmd, FakeContext()),
        ):
            update = FakeUpdate(FakeMessage(), user_id=USER_A)
            await handler(update, ctx)
            self.assertIn("closed to you", update.message.replies[0], handler)
        # Files cannot be stored either.
        doc = _tg_file("tg-x", "x.pdf", 10)
        fmsg = FakeMessage(document=doc, chat_id=USER_A, message_id=9)
        await bot_module.save_file(
            FakeUpdate(fmsg, user_id=USER_A), FakeContext(bot=FakeBot())
        )
        self.assertIn("closed to you", fmsg.replies[0])
        # Plain text too.
        pmsg = FakeMessage(text="hello")
        await bot_module.plain_text(
            FakeUpdate(pmsg, user_id=USER_A), FakeContext()
        )
        self.assertIn("closed to you", pmsg.replies[0])
        # Owner unbans; USER_A is back in.
        msg2 = FakeMessage()
        await bot_module.unban_cmd(FakeUpdate(msg2, user_id=OWNER),
                                   FakeContext(args=[str(USER_A)]))
        self.assertIn("unbanned", msg2.replies[0])
        msg3 = FakeMessage()
        await bot_module.list_cmd(FakeUpdate(msg3, user_id=USER_A),
                                  FakeContext())
        self.assertIn("alice.pdf", msg3.replies[0])

    async def test_ban_is_owner_only_and_owner_cannot_be_banned(self):
        msg = FakeMessage()
        await bot_module.ban_cmd(FakeUpdate(msg, user_id=USER_A),
                                 FakeContext(args=[str(USER_B)]))
        self.assertIn("keeper of this bot only", msg.replies[0])
        self.assertFalse(self.store.is_banned(USER_B))
        msg2 = FakeMessage()
        await bot_module.ban_cmd(FakeUpdate(msg2, user_id=OWNER),
                                 FakeContext(args=[str(OWNER)]))
        self.assertIn("cannot ban", msg2.replies[0])
        self.assertFalse(self.store.is_banned(OWNER))

    # -- (h) /admin ---------------------------------------------------------

    async def test_admin_aggregate_only_and_owner_only(self):
        self._seed(USER_A, filename="alice-secret.pdf")
        self._seed(USER_B, filename="bob-secret.pdf")
        self.store.add_note("alice secret", user_id=USER_A)
        self.store.ensure_user(USER_A)
        self.store.ensure_user(USER_B)
        msg = FakeMessage()
        await bot_module.admin_cmd(FakeUpdate(msg, user_id=OWNER),
                                   FakeContext())
        reply = msg.replies[0]
        self.assertIn("Users: 2", reply)
        self.assertIn("Files: 2", reply)
        self.assertIn("Notes: 1", reply)
        # No names, no contents, no per-user data leak.
        self.assertNotIn("alice-secret", reply)
        self.assertNotIn("bob-secret", reply)
        self.assertNotIn("alice secret", reply)
        # A non-owner is refused.
        msg2 = FakeMessage()
        await bot_module.admin_cmd(FakeUpdate(msg2, user_id=USER_A),
                                   FakeContext())
        self.assertIn("keeper of this bot only", msg2.replies[0])


if __name__ == "__main__":
    unittest.main()
