"""Tests for the security-audit fixes (public multi-user bot).

Run from the project root:
    python -m unittest discover -s tests -v

Covers the audit findings, one test per fix:
  1. HIGH — /ask arithmetic DoS: 9**9**9 and 10**10**10 are refused
     politely (not computed, no hang); real arithmetic still works.
  2. MEDIUM — error_handler never logs message text (a /setkey key
     must never land in the journal).
  3. MEDIUM — the bot works ONLY in private chats: group commands are
     refused before any user row is created; group callback taps are
     refused; /admin in a group is refused.
  5. LOW — oversized numeric ids (OverflowError) get a reply, never
     silence: /get, /note, /delnote, /ban with huge ids.
  6. LOW — notes and active reminders have per-user caps.
  7. LOW — LIKE wildcards in search are escaped: "%" matches only a
     literal "%".
  8. LOW — DELETEs are scoped to the user (defense-in-depth).
  9. LOW — chat_with_ai never silently falls back to the owner's key.
 10. LOW — KeyVault.encrypt fails cleanly (RuntimeError) with no secret.
 11. INFO — a token encrypted with the file secret still decrypts when
     an env secret is also present (so setting SANCTUM_KEY_SECRET later
     does not lock out existing keys).
 12. INFO — requirements.txt pins an upper bound for cryptography.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

# Make the project root importable (bot.py, vault.py, assistant.py, ai.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai as ai_module
import bot as bot_module
import config
import vault as vault_module
from assistant import _try_arithmetic, answer_ask, safe_eval
from keystore import KeyVault
from vault import Store

OWNER = 111
USER_A = 222
USER_B = 333


class FakeMessage:
    def __init__(self, text="", chat_id=111, message_id=1, caption="",
                 document=None, photo=None, video=None, audio=None,
                 voice=None):
        self.text = text
        self.caption = caption
        self.document = document
        self.photo = photo
        self.video = video
        self.audio = audio
        self.voice = voice
        self.chat_id = chat_id
        self.message_id = message_id
        self.date = datetime.now(timezone.utc)
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


class FakeJobQueue:
    """Minimal stand-in for PTB's JobQueue: jobs() + run_once()."""

    def __init__(self):
        self._jobs = []

    def jobs(self):
        return tuple(self._jobs)

    def run_once(self, callback, when, data=None, name=None):
        job = SimpleNamespace(callback=callback, when=when, data=data,
                              name=name)
        self._jobs.append(job)
        return job


class FakeUpdate:
    def __init__(self, message=None, user_id=OWNER, callback_query=None,
                 chat_type="private", chat_id=None):
        self.message = message if message is not None else FakeMessage()
        self.effective_message = self.message
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(
            id=chat_id if chat_id is not None else user_id,
            type=chat_type,
        )
        self.callback_query = callback_query


class FakeContext:
    def __init__(self, args=None, bot=None, job_queue=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=job_queue)
        self.bot = bot


def _store(tmp):
    return Store(Path(tmp) / "data", owner_id=OWNER)


class ArithmeticCapTestCase(unittest.TestCase):
    """(1) HIGH — the arithmetic DoS is gone."""

    def test_huge_powers_are_refused_not_computed(self):
        # These used to wedge the single-threaded bot (9**9**9 is a
        # ~1.2-billion-bit number). They must raise immediately.
        for expr in ("9**9**9", "10**10**10", "2**100000", "10**5000"):
            with self.assertRaises(ValueError, msg=expr):
                safe_eval(expr)

    def test_ask_refuses_huge_powers_politely(self):
        self.assertIn(
            "didn't work",
            _try_arithmetic("what is 9**9**9?") or "",
        )
        self.assertIn(
            "didn't work",
            _try_arithmetic("calculate 10**10**10") or "",
        )

    def test_real_arithmetic_still_works(self):
        self.assertEqual(safe_eval("2+2"), 4)
        self.assertEqual(safe_eval("45 * 12"), 540)
        self.assertEqual(safe_eval("(3+4)*2"), 14)
        self.assertEqual(safe_eval("2**10"), 1024)
        self.assertEqual(safe_eval("10 % 3"), 1)
        self.assertEqual(safe_eval("7 // 2"), 3)
        self.assertAlmostEqual(safe_eval("1/3"), 1 / 3)
        with self.assertRaises(ValueError):
            safe_eval("__import__('os')")
        with self.assertRaises(ValueError):
            safe_eval("open('/etc/passwd')")
        with self.assertRaises(ZeroDivisionError):
            safe_eval("1/0")


class ErrorHandlerTestCase(unittest.IsolatedAsyncioTestCase):
    """(2) MEDIUM — error_handler never logs message text."""

    async def test_no_message_text_in_logs(self):
        captured = []

        class ListHandler(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        handler = ListHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("sanctum")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            update = SimpleNamespace(
                update_id=99,
                effective_user=SimpleNamespace(id=USER_A),
                effective_chat=SimpleNamespace(id=USER_A),
                message=FakeMessage(text="/setkey FAKEKEY-12345-SECRET"),
            )
            context = SimpleNamespace(error=RuntimeError("boom"))
            await bot_module.error_handler(update, context)
        finally:
            logger.removeHandler(handler)
        blob = "\n".join(captured)
        # The secret must not appear anywhere in the log output.
        self.assertNotIn("FAKEKEY-12345-SECRET", blob)
        self.assertNotIn("/setkey", blob)
        # But the useful ids and the error type are logged.
        self.assertIn("99", blob)
        self.assertIn(str(USER_A), blob)
        self.assertIn("RuntimeError", blob)


class PrivateChatTestCase(unittest.IsolatedAsyncioTestCase):
    """(3) MEDIUM — the bot works ONLY in private chats."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)
        self.old_store = bot_module.STORE
        bot_module.STORE = self.store

    def tearDown(self):
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def _group_update(self, user_id=USER_A):
        return FakeUpdate(FakeMessage(), user_id=user_id,
                          chat_type="group", chat_id=-100123)

    async def test_group_command_is_refused_and_creates_no_user(self):
        self.store.add_file(file_id="a1", filename="alice.pdf",
                            file_type="document", size=100,
                            telegram_file_id="a1", user_id=USER_A)
        update = self._group_update()
        await bot_module.list_cmd(update, FakeContext())
        self.assertEqual(len(update.message.replies), 1)
        self.assertIn("private chat", update.message.replies[0])
        self.assertNotIn("alice.pdf", update.message.replies[0])
        # No user row was created for the group interaction.
        self.assertEqual(self.store.count_users(), 0)

    async def test_group_search_and_get_are_refused(self):
        self.store.add_file(file_id="a1", filename="alice.pdf",
                            file_type="document", size=100,
                            telegram_file_id="a1", user_id=USER_A)
        for handler, ctx in (
            (bot_module.search_cmd, FakeContext(args=["alice"])),
            (bot_module.get_cmd, FakeContext(args=["1"])),
            (bot_module.stats_cmd, FakeContext()),
            (bot_module.help_cmd, FakeContext()),
        ):
            update = self._group_update()
            await handler(update, ctx)
            self.assertIn("private chat", update.message.replies[0], handler)

    async def test_group_callback_tap_is_refused(self):
        cb = FakeCallbackQuery("list:1", user_id=USER_A)
        update = FakeUpdate(callback_query=cb, user_id=USER_A,
                            chat_type="group", chat_id=-100123)
        uid = await bot_module._callback_allowed(update)
        self.assertIsNone(uid)
        self.assertTrue(cb.answered)
        self.assertIn("private chat", cb.edits[0])

    async def test_group_clear_confirmation_cannot_be_tapped(self):
        # A /clear confirmation keyboard that somehow ended up in a group
        # must not wipe anything when tapped there.
        self.store.add_file(file_id="a1", filename="alice.pdf",
                            file_type="document", size=100,
                            telegram_file_id="a1", user_id=USER_A)
        cb = FakeCallbackQuery("clear:yes", user_id=USER_A)
        update = FakeUpdate(callback_query=cb, user_id=USER_A,
                            chat_type="group", chat_id=-100123)
        await bot_module.clear_confirm(update, FakeContext())
        self.assertIn("private chat", cb.edits[0])
        self.assertEqual(
            self.store.list_files(limit=10, user_id=USER_A)[1], 1
        )

    async def test_admin_in_group_is_refused(self):
        msg = FakeMessage()
        update = FakeUpdate(msg, user_id=OWNER,
                            chat_type="supergroup", chat_id=-100999)
        await bot_module.admin_cmd(update, FakeContext())
        self.assertIn("private chat", msg.replies[0])

    async def test_private_chat_still_works(self):
        self.store.add_file(file_id="a1", filename="alice.pdf",
                            file_type="document", size=100,
                            telegram_file_id="a1", user_id=USER_A)
        update = FakeUpdate(FakeMessage(), user_id=USER_A,
                            chat_type="private")
        await bot_module.list_cmd(update, FakeContext())
        self.assertIn("alice.pdf", update.message.replies[0])


class OversizedIdTestCase(unittest.IsolatedAsyncioTestCase):
    """(5) LOW — oversized numeric ids get a reply, never silence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)
        self.old_store = bot_module.STORE
        self.old_owner = config.OWNER_ID
        config.OWNER_ID = OWNER
        bot_module.STORE = self.store

    def tearDown(self):
        config.OWNER_ID = self.old_owner
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def test_store_level_huge_ids_are_not_found(self):
        self.store.add_file(file_id="a1", filename="alice.pdf",
                            file_type="document", size=100,
                            telegram_file_id="a1", user_id=USER_A)
        for huge in (10**30, 2**63, -(2**63) - 1, 10**100):
            self.assertIsNone(self.store.get_file(huge, user_id=USER_A))
            self.assertIsNone(self.store.get_note(huge, user_id=USER_A))
            self.assertEqual(self.store.resolve(str(huge), user_id=USER_A), [])
        # A 5000-digit string (past Python's int() conversion limit).
        self.assertEqual(
            self.store.resolve("9" * 5000, user_id=USER_A), []
        )
        # The real file is still there.
        self.assertIsNotNone(self.store.get_file(1, user_id=USER_A))

    async def test_get_with_huge_id_replies_not_found(self):
        self.store.add_file(file_id="a1", filename="alice.pdf",
                            file_type="document", size=100,
                            telegram_file_id="a1", user_id=USER_A)
        msg = FakeMessage()
        await bot_module.get_cmd(
            FakeUpdate(msg, user_id=USER_A),
            FakeContext(args=["9" * 40]),
        )
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("No file matches", msg.replies[0])

    async def test_note_with_huge_id_replies_not_found(self):
        self.store.add_note("a note", user_id=USER_A)
        msg = FakeMessage()
        await bot_module.note_cmd(
            FakeUpdate(msg, user_id=USER_A),
            FakeContext(args=["9" * 40]),
        )
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("No note #", msg.replies[0])

    async def test_delnote_and_ban_with_huge_id_show_usage(self):
        msg = FakeMessage()
        await bot_module.delnote_cmd(
            FakeUpdate(msg, user_id=USER_A),
            FakeContext(args=["9" * 40]),
        )
        self.assertIn("Usage: /delnote", msg.replies[0])
        msg2 = FakeMessage()
        await bot_module.ban_cmd(
            FakeUpdate(msg2, user_id=OWNER),
            FakeContext(args=["9" * 40]),
        )
        self.assertIn("Usage: /ban", msg2.replies[0])

    async def test_remind_with_huge_minutes_shows_usage(self):
        msg = FakeMessage()
        await bot_module.remind_cmd(
            FakeUpdate(msg, user_id=USER_A),
            FakeContext(args=["9" * 40, "x"], job_queue=FakeJobQueue()),
        )
        self.assertIn("Usage: /remind", msg.replies[0])


class CapsTestCase(unittest.IsolatedAsyncioTestCase):
    """(6) LOW — notes and active reminders have per-user caps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)
        self.old_store = bot_module.STORE
        bot_module.STORE = self.store
        self._old_notes = vault_module.MAX_NOTES_PER_USER
        # The notes cap is enforced in vault.py; the reminders cap is
        # enforced in bot.py (it imported the constant) — patch both.
        self._old_reminders_vault = vault_module.MAX_REMINDERS_PER_USER
        self._old_reminders_bot = bot_module.MAX_REMINDERS_PER_USER

    def tearDown(self):
        vault_module.MAX_NOTES_PER_USER = self._old_notes
        vault_module.MAX_REMINDERS_PER_USER = self._old_reminders_vault
        bot_module.MAX_REMINDERS_PER_USER = self._old_reminders_bot
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def _set_reminder_cap(self, n):
        vault_module.MAX_REMINDERS_PER_USER = n
        bot_module.MAX_REMINDERS_PER_USER = n

    def test_notes_cap(self):
        vault_module.MAX_NOTES_PER_USER = 2
        self.store.add_note("one", user_id=USER_A)
        self.store.add_note("two", user_id=USER_A)
        with self.assertRaises(ValueError) as cm:
            self.store.add_note("three", user_id=USER_A)
        self.assertIn("limit", str(cm.exception))
        # Another user is unaffected.
        self.store.add_note("bob's note", user_id=USER_B)

    async def test_note_command_reports_the_cap(self):
        vault_module.MAX_NOTES_PER_USER = 1
        self.store.add_note("one", user_id=USER_A)
        msg = FakeMessage()
        await bot_module.note_cmd(
            FakeUpdate(msg, user_id=USER_A),
            FakeContext(args=["two"]),
        )
        self.assertIn("limit", msg.replies[0])
        self.assertEqual(self.store.list_notes(user_id=USER_A)[1], 1)

    async def test_reminders_cap(self):
        self._set_reminder_cap(2)
        jq = FakeJobQueue()
        for i in range(2):
            jq.run_once(lambda c: None, when=60,
                        data={"chat_id": USER_A, "text": f"r{i}"},
                        name=f"remind-{USER_A}-5")
        msg = FakeMessage()
        await bot_module.remind_cmd(
            FakeUpdate(msg, user_id=USER_A),
            FakeContext(args=["5", "third"], job_queue=jq),
        )
        self.assertIn("2 active reminders", msg.replies[0])
        self.assertEqual(len(jq.jobs()), 2)  # nothing scheduled

    async def test_reminder_under_the_cap_is_scheduled(self):
        self._set_reminder_cap(2)
        jq = FakeJobQueue()
        jq.run_once(lambda c: None, when=60,
                    data={"chat_id": USER_A, "text": "first"},
                    name=f"remind-{USER_A}-5")
        msg = FakeMessage()
        await bot_module.remind_cmd(
            FakeUpdate(msg, user_id=USER_A),
            FakeContext(args=["5", "second"], job_queue=jq),
        )
        self.assertIn("I will remind you", msg.replies[0])
        self.assertEqual(len(jq.jobs()), 2)


class LikeEscapingTestCase(unittest.TestCase):
    """(7) LOW — LIKE wildcards in search are literal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed(self):
        # Note: filenames are sanitized on storage (safe_filename), so
        # "%" can never appear in a stored filename — it CAN appear in
        # captions and notes, which are stored raw. "_" is allowed in
        # filenames, so it is tested there.
        self.store.add_file(file_id="p1", filename="report.pdf",
                            file_type="document", size=1,
                            telegram_file_id="p1", caption="100% sure",
                            user_id=USER_A)
        self.store.add_file(file_id="p2", filename="normal.pdf",
                            file_type="document", size=1,
                            telegram_file_id="p2", user_id=USER_A)
        self.store.add_file(file_id="p3", filename="under_score.pdf",
                            file_type="document", size=1,
                            telegram_file_id="p3", user_id=USER_A)
        self.store.add_note("100% sure", user_id=USER_A)
        self.store.add_note("plain note", user_id=USER_A)

    def test_percent_matches_only_a_literal_percent(self):
        self._seed()
        found = self.store.search_files("%", user_id=USER_A)
        self.assertEqual([f.filename for f in found], ["report.pdf"])
        found = self.store.resolve("%", user_id=USER_A)
        self.assertEqual([f.filename for f in found], ["report.pdf"])

    def test_underscore_matches_only_a_literal_underscore(self):
        self._seed()
        found = self.store.search_files("_", user_id=USER_A)
        self.assertEqual([f.filename for f in found], ["under_score.pdf"])

    def test_backslash_in_search_is_literal(self):
        self._seed()
        found = self.store.search_files("100\\%", user_id=USER_A)
        self.assertEqual(found, [])
        found = self.store.search_files("100%", user_id=USER_A)
        self.assertEqual([f.filename for f in found], ["report.pdf"])

    def test_notes_search_escapes_too(self):
        self._seed()
        found = self.store.search_notes("%", user_id=USER_A)
        self.assertEqual(len(found), 1)
        self.assertIn("100% sure", found[0].text)

    def test_plain_search_still_works(self):
        self._seed()
        self.assertEqual(len(self.store.search_files("normal", user_id=USER_A)), 1)
        self.assertEqual(len(self.store.search_files("pdf", user_id=USER_A)), 3)


class ScopedDeleteTestCase(unittest.TestCase):
    """(8) LOW — DELETE statements are scoped to the user."""

    def test_delete_statements_contain_user_scoping(self):
        source = Path(__file__).resolve().parent.parent.joinpath(
            "vault.py"
        ).read_text()
        self.assertIn("DELETE FROM files WHERE id = ? AND user_id = ?",
                      source)
        self.assertIn("DELETE FROM notes WHERE id = ? AND user_id = ?",
                      source)

    def test_delete_wrong_user_leaves_the_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            try:
                rec = store.add_file(
                    file_id="a1", filename="alice.pdf",
                    file_type="document", size=1,
                    telegram_file_id="a1", user_id=USER_A,
                )
                note = store.add_note("alice note", user_id=USER_A)
                self.assertIsNone(store.delete_file(rec.id, user_id=USER_B))
                self.assertIsNone(store.delete_note(note.id, user_id=USER_B))
                self.assertIsNotNone(
                    store.get_file(rec.id, user_id=USER_A)
                )
                self.assertIsNotNone(
                    store.get_note(note.id, user_id=USER_A)
                )
            finally:
                store.close()


class NoOwnerKeyFallbackTestCase(unittest.IsolatedAsyncioTestCase):
    """(9) LOW — chat_with_ai never silently uses the owner's key."""

    def setUp(self):
        self.old_key = config.GEMINI_API_KEY
        self._orig_generate = ai_module._generate_rest
        ai_module._HISTORY.clear()

    def tearDown(self):
        config.GEMINI_API_KEY = self.old_key
        ai_module._generate_rest = self._orig_generate
        ai_module._HISTORY.clear()

    def _forbid_api(self):
        async def _boom(*a, **k):
            raise AssertionError("Gemini must not be called here")
        ai_module._generate_rest = _boom

    def test_is_ai_available_needs_an_explicit_key(self):
        config.GEMINI_API_KEY = "owner-key"
        self.assertFalse(ai_module.is_ai_available())
        self.assertFalse(ai_module.is_ai_available(None))
        self.assertTrue(ai_module.is_ai_available("some-key"))

    async def test_chat_with_ai_without_explicit_key_never_calls_api(self):
        config.GEMINI_API_KEY = "owner-key"
        self._forbid_api()
        out = await ai_module.chat_with_ai(USER_A, "hello")
        self.assertIn("no Gemini key", out)

    async def test_chat_with_ai_with_explicit_key_calls_api(self):
        config.GEMINI_API_KEY = "owner-key"
        calls = []

        async def _fake(api_key, model_name, history, message):
            calls.append(api_key)
            return "reply"

        ai_module._generate_rest = _fake
        out = await ai_module.chat_with_ai(
            USER_A, "hello", api_key="user-key"
        )
        self.assertEqual(out, "reply")
        self.assertEqual(calls, ["user-key"])


class KeyVaultNoSecretTestCase(unittest.TestCase):
    """(10) LOW — KeyVault.encrypt fails cleanly with no secret."""

    def test_encrypt_without_any_secret_raises_runtime_error(self):
        # A data dir that is a FILE (not a directory): the file secret
        # cannot be created, and no env secret is given — so the vault
        # has no secret at all.
        with tempfile.TemporaryDirectory() as tmp:
            not_a_dir = Path(tmp) / "not-a-dir"
            not_a_dir.write_text("x")
            vault = KeyVault(not_a_dir, env_secret=None)
            with self.assertRaises(RuntimeError) as cm:
                vault.encrypt("AIzaSyD-some-key")
            self.assertIn("no encryption secret", str(cm.exception))
            # decrypt() still fails soft (None), never a crash.
            self.assertIsNone(vault.decrypt("anything"))

    def test_encrypt_with_env_secret_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = KeyVault(Path(tmp) / "data", env_secret="env-secret")
            token = vault.encrypt("AIzaSyD-some-key")
            self.assertEqual(vault.decrypt(token), "AIzaSyD-some-key")

    def test_file_secret_with_whitespace_bytes_survives_reopen(self):
        # Regression test for a latent keystore bug: the file secret is
        # 32 raw random bytes, and the old read path stripped them — so
        # whenever the bytes happened to start or end with an ASCII
        # whitespace byte, every linked key became undecryptable after
        # the bot restarted. A secret with whitespace ends must still
        # decrypt after a fresh KeyVault is opened on the same directory.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            (data_dir / "key_secret").write_bytes(
                b"\n\t" + bytes(range(1, 29)) + b" \r"
            )
            first = KeyVault(data_dir, env_secret=None)
            token = first.encrypt("AIzaSyD-user-key-0123456789")
            second = KeyVault(data_dir, env_secret=None)
            self.assertEqual(
                second.decrypt(token), "AIzaSyD-user-key-0123456789"
            )


class KeySecretCompatibilityTestCase(unittest.TestCase):
    """(11) INFO — setting SANCTUM_KEY_SECRET later must not lock out
    keys encrypted with the file secret (decryption tries every secret)."""

    def test_file_secret_token_still_readable_with_env_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            # First vault: file secret only (as deployed today).
            first = KeyVault(data_dir, env_secret=None)
            token = first.encrypt("AIzaSyD-user-key-0123456789")
            # Second vault: env secret added later (finding #11) — the
            # file secret is still on disk and still tried.
            second = KeyVault(data_dir, env_secret="fresh-env-secret")
            self.assertEqual(second.decrypt(token), "AIzaSyD-user-key-0123456789")
            # And a key encrypted with the new env secret also works.
            token2 = second.encrypt("AIzaSyD-other-key-0123456789")
            self.assertEqual(second.decrypt(token2), "AIzaSyD-other-key-0123456789")


class RequirementsPinTestCase(unittest.TestCase):
    """(12) INFO — cryptography has a pinned upper bound."""

    def test_cryptography_upper_bound_pinned(self):
        req = Path(__file__).resolve().parent.parent.joinpath(
            "requirements.txt"
        ).read_text()
        for line in req.splitlines():
            if line.startswith("cryptography"):
                self.assertIn("<", line)
                self.assertRegex(line, r"cryptography>=42,<\d+")


if __name__ == "__main__":
    unittest.main()
