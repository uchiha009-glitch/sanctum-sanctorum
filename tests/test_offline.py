"""Offline tests for Sanctum Sanctorum — no token, no Telegram calls.

Run from the project root:
    python -m unittest discover -s tests -v

Covers: vault storage (add/list/search/get/delete/stats), notes,
the rule-based /ask responder, reminder scheduling + delivery (fakes),
and the owner gate / config validation.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Make the project root importable (bot.py, vault.py, assistant.py, config.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import answer_ask, reminder_callback, safe_eval, schedule_reminder
from vault import Store, human_size


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_and_get_file(self):
        rec = self.store.add_file(
            file_id="tg-1", filename="report.pdf", file_type="document",
            size=5, caption="exam notes", data=b"hello", user_id=111)
        self.assertEqual(rec.filename, "report.pdf")
        self.assertEqual(rec.category, "documents")
        self.assertTrue(rec.path.exists())
        self.assertEqual(rec.path.read_bytes(), b"hello")
        fetched = self.store.get_file(rec.id, user_id=111)
        self.assertEqual(fetched.filename, "report.pdf")

    def test_categories(self):
        cases = {
            "document": "documents", "photo": "photos",
            "video": "videos", "audio": "audio", "voice": "audio",
        }
        for ftype, category in cases.items():
            rec = self.store.add_file(
                file_id=f"tg-{ftype}", filename=f"f.{ftype}",
                file_type=ftype, data=b"x", user_id=111)
            self.assertEqual(rec.category, category)
            self.assertTrue((rec.path.parent.name == category))

    def test_list_search_resolve(self):
        a = self.store.add_file(file_id="1", filename="anatomy.pdf",
                                file_type="document", data=b"a", user_id=111)
        b = self.store.add_file(file_id="2", filename="anatomy_notes.pdf",
                                file_type="document", caption="physio",
                                data=b"b", user_id=111)
        self.store.add_file(file_id="3", filename="song.mp3",
                            file_type="audio", data=b"c", user_id=111)
        records, total = self.store.list_files(limit=10, user_id=111)
        self.assertEqual(total, 3)
        self.assertEqual(records[0].id, 3)  # newest first
        self.assertEqual(len(self.store.search_files("ANATOMY", user_id=111)), 2)  # case-insensitive
        self.assertEqual(len(self.store.search_files("physio", user_id=111)), 1)
        self.assertEqual(self.store.resolve(str(a.id), user_id=111)[0].id, a.id)
        ambiguous = self.store.resolve("anatomy", user_id=111)
        self.assertEqual(len(ambiguous), 2)
        self.assertEqual(self.store.resolve("no-such-file", user_id=111), [])

    def test_delete_removes_disk_and_index(self):
        rec = self.store.add_file(file_id="1", filename="gone.txt",
                                  file_type="document", data=b"x", user_id=111)
        path = rec.path
        self.assertTrue(path.exists())
        deleted = self.store.delete_file(rec.id, user_id=111)
        self.assertEqual(deleted.id, rec.id)
        self.assertFalse(path.exists())
        self.assertIsNone(self.store.get_file(rec.id, user_id=111))
        self.assertIsNone(self.store.delete_file(9999, user_id=111))

    def test_stats(self):
        self.store.add_file(file_id="1", filename="a.pdf", file_type="document",
                            size=100, data=b"x" * 100, user_id=111)
        self.store.add_file(file_id="2", filename="b.jpg", file_type="photo",
                            size=200, data=b"y" * 200, user_id=111)
        s = self.store.stats(user_id=111)
        self.assertEqual(s["total_files"], 2)
        self.assertEqual(s["total_size"], 300)
        self.assertEqual(s["by_category"]["documents"]["count"], 1)
        self.assertEqual(s["by_category"]["photos"]["size"], 200)
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(500), "500 B")

    def test_notes_crud(self):
        n1 = self.store.add_note("Buy milk", user_id=111)
        n2 = self.store.add_note("Exam on Friday — anatomy", user_id=111)
        notes, total = self.store.list_notes(user_id=111)
        self.assertEqual(total, 2)
        self.assertEqual(notes[0].id, n2.id)  # newest first
        self.assertEqual(self.store.get_note(n1.id, user_id=111).text, "Buy milk")
        self.assertEqual(len(self.store.search_notes("anatomy", user_id=111)), 1)
        self.assertEqual(len(self.store.search_notes("ANATOMY", user_id=111)), 1)
        deleted = self.store.delete_note(n1.id, user_id=111)
        self.assertEqual(deleted.id, n1.id)
        self.assertIsNone(self.store.get_note(n1.id, user_id=111))
        with self.assertRaises(ValueError):
            self.store.add_note("   ", user_id=111)

    def test_add_file_by_reference_writes_nothing_to_disk(self):
        """Reference-only add (no data/path): the vault keeps the file_id
        and source message, and writes nothing to disk. This replaced the
        old download-to-disk behaviour."""
        rec = self.store.add_file(
            file_id="tg-big", filename="vibes.apk", file_type="document",
            size=110 * 1024 * 1024, telegram_file_id="tg-big",
            source_chat_id=111, source_message_id=42, user_id=111)
        self.assertEqual(rec.telegram_file_id, "tg-big")
        self.assertEqual(rec.source_chat_id, 111)
        self.assertEqual(rec.source_message_id, 42)
        self.assertTrue(rec.stored_by_reference)
        self.assertEqual(rec.stored_name, "")
        # No on-disk file exists for this record (path points at the
        # category directory itself, which is not a file).
        self.assertFalse(rec.path.is_file())
        # Nothing was written under the data dir except the index itself
        # and the key secret.
        data_dir = Path(self.tmp.name) / "data"
        stored = [p for p in data_dir.rglob("*")
                  if p.is_file() and p.name not in ("vault.db", "key_secret")]
        self.assertEqual(stored, [])
        fetched = self.store.get_file(rec.id, user_id=111)
        self.assertEqual(fetched.telegram_file_id, "tg-big")


class AskTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.store.add_file(file_id="1", filename="anatomy.pdf",
                            file_type="document", size=10, data=b"x" * 10, user_id=111)
        self.store.add_note("Exam on Friday — anatomy revision", user_id=111)
        self.store.add_note("Buy dates and almonds", user_id=111)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_arithmetic(self):
        self.assertEqual(answer_ask("2+2", self.store, 111), "2+2 = 4")
        self.assertEqual(answer_ask("what is 45 * 12?", self.store, 111), "45 * 12 = 540")
        self.assertEqual(answer_ask("calculate (3+4)*2", self.store, 111), "(3+4)*2 = 14")
        self.assertIn("didn't work", answer_ask("what is 1/0?", self.store, 111))
        with self.assertRaises(ValueError):
            safe_eval("__import__('os')")
        with self.assertRaises(ValueError):
            safe_eval("open('/etc/passwd')")

    def test_vault_questions(self):
        out = answer_ask("how many files do I have?", self.store, 111)
        self.assertIn("1 file(s)", out)
        self.assertIn("documents", out)
        out = answer_ask("what files do I have?", self.store, 111)
        self.assertIn("anatomy.pdf", out)

    def test_notes_questions(self):
        out = answer_ask("what notes do I have about anatomy?", self.store, 111)
        self.assertIn("anatomy revision", out)
        out = answer_ask("what notes do I have?", self.store, 111)
        self.assertIn("Buy dates", out)
        out = answer_ask("what notes do I have about cooking?", self.store, 111)
        self.assertIn("No notes about", out)

    def test_time_and_identity(self):
        self.assertIn("It is", answer_ask("what time is it?", self.store, 111))
        self.assertIn("Today is", answer_ask("what date is it?", self.store, 111))
        self.assertIn("Sanctum Sanctorum", answer_ask("who are you?", self.store, 111))
        self.assertIn("no external AI", answer_ask("what can you do?", self.store, 111))

    def test_honest_fallback(self):
        out = answer_ask("tell me about quantum physics", self.store, 111)
        self.assertIn("no external AI", out)
        self.assertIn("/list", out)


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, data=None, name=None):
        job = SimpleNamespace(callback=callback, when=when, data=data, name=name)
        self.jobs.append(job)
        return job


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class ReminderTestCase(unittest.IsolatedAsyncioTestCase):
    def test_schedule_reminder(self):
        jq = FakeJobQueue()
        job = schedule_reminder(jq, chat_id=111, minutes=30, text="drink water")
        self.assertEqual(len(jq.jobs), 1)
        self.assertEqual(job.when, 30 * 60)
        self.assertEqual(job.data["chat_id"], 111)
        self.assertEqual(job.data["text"], "drink water")
        self.assertIs(job.callback, reminder_callback)

    async def test_reminder_callback_delivers(self):
        bot = FakeBot()
        context = SimpleNamespace(
            job=SimpleNamespace(data={"chat_id": 111, "text": "drink water"}),
            bot=bot,
        )
        await reminder_callback(context)
        self.assertEqual(bot.sent, [(111, "⏰ Reminder: drink water")])


class OwnerGateTestCase(unittest.TestCase):
    def test_is_owner_and_validate(self):
        import bot as bot_module
        import config

        old_token, old_owner = config.BOT_TOKEN, config.OWNER_ID
        try:
            config.BOT_TOKEN = None
            config.OWNER_ID = None
            with self.assertRaises(RuntimeError):
                config.validate()
            config.BOT_TOKEN = "dummy-token"
            config.OWNER_ID = 111
            self.assertTrue(bot_module.is_owner(111))
            self.assertFalse(bot_module.is_owner(222))
            self.assertFalse(bot_module.is_owner(None))
        finally:
            config.BOT_TOKEN, config.OWNER_ID = old_token, old_owner


if __name__ == "__main__":
    unittest.main()
