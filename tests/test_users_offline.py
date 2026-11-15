"""Offline tests for owner-only /users and last-seen tracking.

Run from the project root:
    python -m unittest discover -s tests -v

Covers:
  * /users is owner-only: a non-owner gets the keeper-only refusal
    and sees no user data; /users is on the owner menu (14) only, and
    never on the default menu (9) or in non-owner /help;
  * the owner's list shows one line per user — live name (resolved
    via get_chat, never stored), Telegram id, first-seen date, last
    seen, and file/note/link counts — ordered oldest first;
  * a get_chat failure falls back to an id-only line for that user
    and never breaks the rest of the list;
  * the list caps at 25 users with an "…and N more users" line;
  * last_seen: migration adds the column to an old DB without one
    (0 = never tracked); it advances after a command through
    require_user and after a button tap; /users shows "—" for 0 and a
    real IST timestamp after the user interacts.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

# Make the project root importable (bot.py, vault.py, config.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as bot_module
import config
from vault import Store

OWNER = 111
FRIEND = 222
OTHER = 333

IST = timezone(timedelta(hours=5, minutes=30))


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


class FakeChat:
    def __init__(self, first_name=None, last_name=None, username=None):
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class FakeBot:
    """Stands in for telegram.Bot.get_chat. `names` maps user id ->
    FakeChat; `fail_for` is the set of ids whose lookup raises."""

    def __init__(self, names=None, fail_for=()):
        self.names = names or {}
        self.fail_for = set(fail_for)

    async def get_chat(self, user_id):
        if user_id in self.fail_for:
            raise Exception("Forbidden: bot was blocked by the user")
        return self.names.get(
            user_id, FakeChat(first_name=f"User{user_id}")
        )


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.application = SimpleNamespace(job_queue=None)
        self.bot = bot


def _seed_two_users(store):
    """Owner + friend with distinct vaults, mirroring the live vault:
    owner 9 files / 1 note / 2 links, friend 3 files / 1 note / 0 links.
    Seeding writes rows directly — it does NOT touch last_seen, so
    both users start at 0 (never tracked) until they interact."""
    store.ensure_user(OWNER)
    store.ensure_user(FRIEND)
    for i in range(9):
        store.add_file(file_id=f"o{i}", filename=f"owner{i}.txt",
                       file_type="document", data=b"x", user_id=OWNER)
    for i in range(3):
        store.add_file(file_id=f"f{i}", filename=f"friend{i}.jpg",
                       file_type="photo", data=b"y", user_id=FRIEND)
    store.add_note("owner note", user_id=OWNER)
    store.add_note("friend note", user_id=FRIEND)
    store.add_link("https://example.com/a", "first", user_id=OWNER)
    store.add_link("https://example.com/b", "second", user_id=OWNER)


class UsersCommandTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.old_token, self.old_owner = config.BOT_TOKEN, config.OWNER_ID
        self.old_store = bot_module.STORE
        config.BOT_TOKEN = "dummy-token"
        config.OWNER_ID = OWNER
        bot_module.STORE = self.store
        _seed_two_users(self.store)
        self.names = {
            OWNER: FakeChat(first_name="Aseem", username="Hattori009"),
            FRIEND: FakeChat(first_name="Friend", last_name="One",
                             username="friendone"),
        }

    def tearDown(self):
        config.BOT_TOKEN, config.OWNER_ID = self.old_token, self.old_owner
        bot_module.STORE = self.old_store
        self.store.close()
        self.tmp.cleanup()

    async def _run_users(self, user_id=OWNER, bot=None):
        update = FakeUpdate(user_id=user_id)
        await bot_module.users_cmd(
            update, FakeContext(bot=bot or FakeBot(self.names)))
        return update.message.replies[0][0]

    def _line_for(self, text, user_id):
        for line in text.splitlines():
            if f"ID {user_id}" in line:
                return line
        self.fail(f"no line for user {user_id} in:\n{text}")

    async def test_non_owner_is_refused_and_sees_nothing(self):
        text = await self._run_users(user_id=OTHER)
        self.assertIn("keeper of this bot only", text)
        self.assertNotIn("Aseem", text)
        self.assertNotIn("Friend", text)
        self.assertNotIn(str(OWNER), text)
        self.assertNotIn(str(FRIEND), text)

    async def test_owner_lists_both_users_with_counts(self):
        text = await self._run_users()
        self.assertIn("Sanctum users — 2 total", text)
        owner_line = self._line_for(text, OWNER)
        self.assertIn("Aseem (@Hattori009)", owner_line)
        self.assertIn("9 files, 1 note, 2 links", owner_line)
        friend_line = self._line_for(text, FRIEND)
        self.assertIn("Friend One (@friendone)", friend_line)
        self.assertIn("3 files, 1 note, 0 links", friend_line)
        # First-seen dates are today (seeded just now), in IST.
        today = datetime.now(tz=IST).strftime("%d %b %Y")
        self.assertIn(f"since {today}", owner_line)
        self.assertIn(f"since {today}", friend_line)

    async def test_last_seen_is_dash_before_first_interaction(self):
        # Neither seeded user has interacted, so the friend (who never
        # touches the bot in this test) shows "—". The owner just ran
        # /users, so his own line shows a real timestamp instead.
        text = await self._run_users()
        self.assertIn("last seen —", self._line_for(text, FRIEND))
        owner_line = self._line_for(text, OWNER)
        self.assertNotIn("last seen —", owner_line)
        today = datetime.now(tz=IST).strftime("%d %b %Y")
        self.assertIn(f"last seen {today}", owner_line)

    async def test_get_chat_failure_falls_back_to_id_only(self):
        bot = FakeBot(self.names, fail_for={FRIEND})
        text = await self._run_users(bot=bot)
        friend_line = self._line_for(text, FRIEND)
        self.assertNotIn("Friend", friend_line)
        self.assertTrue(friend_line.startswith("• ID 222"),
                        friend_line)
        self.assertIn("3 files", friend_line)
        # The owner's line is unaffected by the friend's failure.
        self.assertIn("Aseem (@Hattori009)",
                      self._line_for(text, OWNER))

    async def test_banned_user_is_marked(self):
        self.store.set_banned(FRIEND, True)
        text = await self._run_users()
        self.assertIn("🚫 banned", self._line_for(text, FRIEND))
        self.assertNotIn("🚫 banned", self._line_for(text, OWNER))

    async def test_list_is_capped_at_25(self):
        for uid in range(1000, 1024):
            self.store.ensure_user(uid)
        # 2 seeded + 24 more = 26 users.
        text = await self._run_users()
        self.assertIn("Sanctum users — 26 total", text)
        bullet_lines = [l for l in text.splitlines()
                        if l.startswith("• ")]
        self.assertEqual(len(bullet_lines), 25)
        self.assertIn("…and 1 more users", text)

    async def test_users_on_owner_menu_only(self):
        self.assertNotIn("users", [c for c, _ in bot_module.BOT_COMMANDS])
        self.assertIn("users", [c for c, _ in bot_module.ADMIN_COMMANDS])
        self.assertEqual(len(bot_module.BOT_COMMANDS), 9)

        class MenuBot:
            def __init__(self):
                self.calls = []

            async def set_my_commands(self, commands, scope=None):
                self.calls.append((commands, scope))

        fake_app = SimpleNamespace(bot=MenuBot())
        await bot_module.set_bot_commands(fake_app)
        default_names = [c.command for c in fake_app.bot.calls[0][0]]
        owner_names = [c.command for c in fake_app.bot.calls[1][0]]
        self.assertEqual(len(default_names), 9)
        self.assertNotIn("users", default_names)
        self.assertEqual(len(owner_names), 14)
        self.assertIn("users", owner_names)

    async def test_help_hides_users_from_non_owners(self):
        update = FakeUpdate(user_id=OTHER)
        await bot_module.help_cmd(update, FakeContext())
        self.assertNotIn("/users", update.message.replies[0][0])
        update = FakeUpdate(user_id=OWNER)
        await bot_module.help_cmd(update, FakeContext())
        self.assertIn("/users", update.message.replies[0][0])


class LastSeenTestCase(unittest.IsolatedAsyncioTestCase):
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

    def test_migration_adds_last_seen_to_old_db(self):
        # A database from before tracking: users table with no
        # last_seen column at all.
        old_path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(old_path)
        conn.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY,"
            " api_key TEXT NOT NULL DEFAULT '',"
            " is_banned INTEGER NOT NULL DEFAULT 0,"
            " created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO users (user_id, created_at)"
            " VALUES (555, 1794743491)"
        )
        conn.commit()
        conn.close()
        store = Store(Path(self.tmp.name) / "old-data", db_path=old_path)
        try:
            cols = {row[1] for row in store._conn.execute(
                "PRAGMA table_info(users)").fetchall()}
            self.assertIn("last_seen", cols)
            user = store.get_user(555)
            self.assertEqual(user["last_seen"], 0)
            self.assertEqual(user["created_at"], 1794743491)
            # Opening again changes nothing (idempotent migration).
            store2 = Store(Path(self.tmp.name) / "old-data",
                           db_path=old_path)
            try:
                self.assertEqual(store2.get_user(555)["last_seen"], 0)
            finally:
                store2.close()
        finally:
            store.close()

    def test_fresh_db_has_last_seen_from_the_start(self):
        cols = {row[1] for row in self.store._conn.execute(
            "PRAGMA table_info(users)").fetchall()}
        self.assertIn("last_seen", cols)

    async def test_last_seen_advances_after_a_command(self):
        self.store.ensure_user(FRIEND)
        self.assertEqual(self.store.get_user(FRIEND)["last_seen"], 0)
        before = int(time.time())
        update = FakeUpdate(user_id=FRIEND)
        await bot_module.help_cmd(update, FakeContext())
        seen = self.store.get_user(FRIEND)["last_seen"]
        self.assertGreaterEqual(seen, before)
        self.assertLessEqual(seen, int(time.time()))

    async def test_last_seen_advances_after_a_button_tap(self):
        self.store.ensure_user(FRIEND)
        self.store.add_file(file_id="f1", filename="pic.jpg",
                            file_type="photo", data=b"p", user_id=FRIEND)
        self.assertEqual(self.store.get_user(FRIEND)["last_seen"], 0)
        before = int(time.time())
        cb = FakeCallbackQuery("list:0", user_id=FRIEND)
        update = FakeUpdate(user_id=FRIEND, callback_query=cb)
        await bot_module.list_page(update, FakeContext())
        self.assertTrue(cb.edits)
        seen = self.store.get_user(FRIEND)["last_seen"]
        self.assertGreaterEqual(seen, before)
        self.assertLessEqual(seen, int(time.time()))

    async def test_last_seen_advances_after_plain_chat(self):
        self.store.ensure_user(FRIEND)
        update = FakeUpdate(text="hello", user_id=FRIEND)
        await bot_module.plain_text(update, FakeContext())
        self.assertGreater(self.store.get_user(FRIEND)["last_seen"], 0)

    async def test_banned_user_is_not_marked_active(self):
        self.store.ensure_user(FRIEND)
        self.store.set_banned(FRIEND, True)
        update = FakeUpdate(user_id=FRIEND)
        await bot_module.help_cmd(update, FakeContext())
        self.assertEqual(self.store.get_user(FRIEND)["last_seen"], 0)

    async def test_users_shows_last_seen_after_interaction(self):
        _seed_two_users(self.store)
        self.store.ensure_user(FRIEND)
        update = FakeUpdate(user_id=FRIEND)
        await bot_module.help_cmd(update, FakeContext())
        update = FakeUpdate(user_id=OWNER)
        await bot_module.users_cmd(
            update, FakeContext(bot=FakeBot({
                OWNER: FakeChat(first_name="Aseem"),
                FRIEND: FakeChat(first_name="Friend"),
            })))
        text = update.message.replies[0][0]
        friend_line = next(
            line for line in text.splitlines() if "ID 222" in line)
        self.assertNotIn("last seen —", friend_line)
        today = datetime.now(tz=IST).strftime("%d %b %Y")
        self.assertIn(f"last seen {today}", friend_line)

    def test_list_users_is_oldest_first(self):
        for uid, created in ((300, 1794743729), (100, 1794743491),
                             (200, 1794743600)):
            self.store.ensure_user(uid)
            self.store._conn.execute(
                "UPDATE users SET created_at = ? WHERE user_id = ?",
                (created, uid),
            )
        self.store._conn.commit()
        self.assertEqual(
            [u["user_id"] for u in self.store.list_users()],
            [100, 200, 300],
        )


if __name__ == "__main__":
    unittest.main()
