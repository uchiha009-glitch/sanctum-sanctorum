"""Sanctum Sanctorum — vault storage layer (PUBLIC multi-user bot).

Every user gets a private vault, isolated by their Telegram user id.
Every file, note, and user record belongs to exactly one user id, and
every query filters by that id — a user can never see, get, search,
delete, or list another user's files or notes, and the owner is just a
user with his own vault.

Files are stored BY REFERENCE: the bot keeps the Telegram file_id, the
file's name, size, and the source message it arrived in — Telegram
itself holds the bytes, nothing is downloaded to disk. A small SQLite
database (<data_dir>/vault.db) holds the index; notes live in the same
database. Per-user Gemini API keys (for /chat, bring-your-own-key) are
stored encrypted (see keystore.py) in the users table.

Older records (from before the file_id switch, and from before the
multi-user switch) were stored on disk under <data_dir>/<category>/ and
belonged to the owner; they keep working from disk and the migration
assigns them to the owner id. No network, no external services: this
module is pure local storage and is fully testable offline.

Categories: documents, photos, videos, audio, apps (APKs & archives,
filed by filename extension — see classify_category).

Per-user limits (anti-abuse; files are references, so these are sums of
the sizes Telegram reports, not disk use):
  MAX_FILES_PER_USER  = 500 files per user
  MAX_BYTES_PER_USER  = 5 GB of reported file sizes per user
  MAX_FILES_PER_DAY   = 30 files stored per user per rolling 24 hours
  MAX_NOTES_PER_USER  = 500 notes per user
  MAX_LINKS_PER_USER  = 500 saved links per user
  MAX_REMINDERS_PER_USER = 20 active (pending) reminders per user
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from keystore import KeyVault

# Telegram file kind -> on-disk category. APKs and archives are the
# exception: Telegram calls an APK a "document", but the vault files it
# under "apps" — see classify_category below.
CATEGORIES = {
    "document": "documents",
    "photo": "photos",
    "video": "videos",
    "audio": "audio",
    "voice": "audio",
}

# File extensions that count as an "app / archive" for the /apps browse
# command (case-insensitive match on the filename).
APP_ARCHIVE_EXTENSIONS = (
    ".apk", ".xapk", ".apks", ".apkm",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
)

# Friendly names for the stored categories, used by /stats and by /ask
# ("how many files do I have?") so both say the same thing: the stored
# category "photos" is shown as "images", and "apps" is explained.
CATEGORY_LABELS = {
    "documents": "documents",
    "photos": "images",
    "videos": "videos",
    "audio": "audio",
    "apps": "apps (APKs & archives)",
}


def classify_category(filename: str, file_type: str) -> str:
    """Store category for an incoming file.

    APKs and archives go to "apps" by filename extension (case-
    insensitive), whatever Telegram called the file — Telegram reports
    an APK as kind "document", but the vault must file it as an app so
    /apps finds it and /docs does not. Everything else follows the
    Telegram file kind. The match is on the extension at the very end
    of the name, so "photo.apk.jpg" stays a document and a bare name
    ending in "tar" (e.g. "avatar") does not become an archive.
    """
    name = (filename or "").lower()
    if name.endswith(APP_ARCHIVE_EXTENSIONS):
        return "apps"
    return CATEGORIES.get(file_type, "documents")

# Per-user limits (anti-abuse). See the module docstring.
MAX_FILES_PER_USER = 500
MAX_BYTES_PER_USER = 5 * 1024 * 1024 * 1024  # 5 GB of reported sizes
MAX_FILES_PER_DAY = 30  # per rolling 24 hours
MAX_NOTES_PER_USER = 500  # notes per user
MAX_LINKS_PER_USER = 500  # saved links per user
MAX_REMINDERS_PER_USER = 20  # active (pending) reminders per user

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    user_num    INTEGER,
    filename    TEXT NOT NULL,
    stored_name TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL,
    file_type   TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    file_id     TEXT NOT NULL,
    caption     TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    text       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    api_key    TEXT NOT NULL DEFAULT '',
    is_banned  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_filename ON files(filename);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_notes_text ON notes(text);
CREATE INDEX IF NOT EXISTS idx_links_url ON links(url);
"""


@dataclass
class FileRecord:
    id: int
    user_id: int
    filename: str
    stored_name: str
    category: str
    file_type: str
    size: int
    file_id: str
    caption: str
    created_at: int
    # Per-user sequence number: each user's vault numbers its own files
    # #1, #2, #3... The global `id` is only the database primary key —
    # users never see it. Every user-facing number is the user_num.
    user_num: int = 0
    # New in the file_id storage model: the Telegram file_id to send the
    # file back with, and the source message it arrived in (so large files
    # can be forwarded back). Old on-disk records have these empty and
    # are served from disk instead.
    telegram_file_id: str = ""
    source_chat_id: int | None = None
    source_message_id: int | None = None

    @property
    def path(self) -> Path:
        """Absolute on-disk path of the stored file (set by Store)."""
        return self._path  # type: ignore[attr-defined]

    @property
    def stored_by_reference(self) -> bool:
        """True if this file lives on Telegram (file_id) rather than disk."""
        return bool(self.telegram_file_id)

    @property
    def date_str(self) -> str:
        """When the file was sent to the bot, e.g. '2026-11-15 14:32'."""
        return datetime.fromtimestamp(self.created_at).strftime("%Y-%m-%d %H:%M")

    def display(self) -> str:
        # The per-user number is what the user sees and types. The
        # `or self.id` fallback only covers a record built without a
        # user_num (never from the Store — the migration fills it).
        return (
            f"#{self.user_num or self.id} {self.filename} ({self.category}, "
            f"{human_size(self.size)}, {self.date_str})"
        )


@dataclass
class NoteRecord:
    id: int
    user_id: int
    text: str
    created_at: int


@dataclass
class LinkRecord:
    id: int
    user_id: int
    url: str
    title: str
    created_at: int

    def display(self) -> str:
        """One line for /links: '#3 Title — https://…' (or just the URL
        when no title was saved). Long URLs are shortened for display;
        the full URL is shown by /links <id>."""
        label = self.title.strip() or self.url
        if len(label) > 80:
            label = label[:77] + "…"
        return f"#{self.id} {label}"


def human_size(n: int) -> str:
    """Format a byte count, e.g. 1536 -> '1.5 KB'."""
    n = max(0, int(n))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def safe_filename(name: str) -> str:
    """Strip directories and unsafe characters from a filename."""
    name = Path(name).name.strip() or "file"
    name = re.sub(r"[^A-Za-z0-9._\- ]+", "_", name)
    name = name.strip(" .")
    return name or "file"


# SQLite integers are signed 64-bit. A numeric id from a user (e.g.
# "/get 99999999999999999999999") must be refused politely instead of
# raising OverflowError inside sqlite3 — and a 5000-digit string must
# not raise ValueError from int() either.
_MAX_SQLITE_INT = 2**63 - 1
_MIN_SQLITE_INT = -(2**63)


def _id_in_range(value: int) -> bool:
    """True when `value` is an int that fits in a signed 64-bit SQLite
    integer."""
    return isinstance(value, int) and _MIN_SQLITE_INT <= value <= _MAX_SQLITE_INT


def _escape_like(pattern: str) -> str:
    """Escape LIKE wildcards so a user search is literal: a search for
    "%" must match only names that literally contain "%", not every
    row. Backslash is the escape character (see ESCAPE '\\' in the
    queries below)."""
    return (
        pattern.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _like_pattern(query: str) -> str:
    """A LIKE pattern for a user search: escaped, wrapped in %...%."""
    return f"%{_escape_like((query or '').strip())}%"


class Store:
    """SQLite index + on-disk file storage for the vault and notes.

    Every method that touches files or notes takes a required `user_id`
    and only ever sees that user's rows. There is deliberately no
    unscoped query path — forgetting the user id is a TypeError, not a
    leak.

    `owner_id` is used ONLY by the migration: rows created before the
    multi-user switch have no user_id, and they belonged to the owner,
    so the migration assigns them to the owner id. It is idempotent
    (only rows with a NULL user_id are assigned).
    """

    def __init__(self, data_dir: str | Path, db_path: str | Path | None = None,
                 owner_id: int | None = None, key_secret: str | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # "apps" is not a Telegram kind, so it is not in CATEGORIES —
        # it still needs its on-disk directory for legacy disk records.
        for category in set(CATEGORIES.values()) | {"apps"}:
            (self.data_dir / category).mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else self.data_dir / "vault.db"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._owner_id = owner_id
        self._migrate()
        self._conn.commit()
        self.key_vault = KeyVault(self.data_dir, env_secret=key_secret)

    def _migrate(self) -> None:
        """Bring an older database up to the current schema. Runs on
        every open, so it is safe to call repeatedly.

        * Adds the file_id-storage columns (telegram_file_id,
          source_chat_id, source_message_id) if missing — old on-disk
          records keep working with those columns empty.
        * Adds user_id to files and notes if missing, and assigns any
          rows that have no user_id to the owner id (they predate the
          multi-user switch and belonged to the owner). Only NULL rows
          are touched, so re-opening changes nothing.
        * Adds user_num to files if missing, and assigns per-user
          sequence numbers to any row that has none (NULL or 0): each
          user's files are numbered in id order, starting at 1, so the
          owner's old global #3-#7 become his #1-#5 and another user's
          first file becomes their #1. Only unset rows are touched.
        * Re-files APKs/archives that were stored as "documents" before
          the extension-based classification: any row still labelled
          "documents" whose filename ends in an app/archive extension
          moves to "apps". Plain documents never move, and the match is
          on ".<ext>" at the very end, so "photo.apk.jpg" stays put.
        * Drops duplicate rows (see dedupe_files): the same Telegram
          file_id stored twice for one user. The earliest row is kept,
          the rest are removed, and that user's files are renumbered
          gaplessly, so the vault reads #1..#N again.
        * Adds last_seen to users if missing — a timestamp only, never
          any message content. Rows created before tracking keep 0
          until their user next interacts.
        """
        file_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(files)").fetchall()
        }
        if "telegram_file_id" not in file_cols:
            self._conn.execute(
                "ALTER TABLE files ADD COLUMN telegram_file_id TEXT NOT NULL DEFAULT ''"
            )
        if "source_chat_id" not in file_cols:
            self._conn.execute(
                "ALTER TABLE files ADD COLUMN source_chat_id INTEGER"
            )
        if "source_message_id" not in file_cols:
            self._conn.execute(
                "ALTER TABLE files ADD COLUMN source_message_id INTEGER"
            )
        if "user_id" not in file_cols:
            self._conn.execute("ALTER TABLE files ADD COLUMN user_id INTEGER")
        if "user_num" not in file_cols:
            self._conn.execute("ALTER TABLE files ADD COLUMN user_num INTEGER")
        note_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(notes)").fetchall()
        }
        if "user_id" not in note_cols:
            self._conn.execute("ALTER TABLE notes ADD COLUMN user_id INTEGER")
        user_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "last_seen" not in user_cols:
            # Users who joined before last-seen tracking keep 0 until
            # they next interact — shown as "—" in /users, never a
            # made-up date.
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN last_seen INTEGER NOT NULL DEFAULT 0"
            )
        # Per-user indexes, created only now that the columns exist
        # (a legacy database has no user_id column until the ALTERs above).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_user"
            " ON files(user_id, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notes_user"
            " ON notes(user_id, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_links_user"
            " ON links(user_id, created_at)"
        )
        if self._owner_id is not None:
            self._conn.execute(
                "UPDATE files SET user_id = ? WHERE user_id IS NULL",
                (self._owner_id,),
            )
            self._conn.execute(
                "UPDATE notes SET user_id = ? WHERE user_id IS NULL",
                (self._owner_id,),
            )
        # Per-user numbering backfill (runs AFTER the user_id assignment
        # above, so pre-multi-user rows belong to the owner first). A
        # row's number is its position among its own user's files in id
        # order — 1-based, no gaps, no reuse of another user's numbers.
        self._conn.execute(
            "UPDATE files SET user_num = ("
            "  SELECT COUNT(*) FROM files f2"
            "  WHERE f2.user_id = files.user_id AND f2.id <= files.id"
            ") WHERE user_num IS NULL OR user_num = 0"
        )
        # Re-file APKs/archives that were stored as "documents" before
        # extension-based classification (see classify_category). Only
        # rows still labelled "documents" move, and only when the
        # filename ends in ".<ext>" — the extensions are fixed strings
        # with no LIKE wildcards in them, so no escaping is needed.
        for ext in APP_ARCHIVE_EXTENSIONS:
            self._conn.execute(
                "UPDATE files SET category = 'apps' WHERE category = ?"
                " AND filename LIKE ? COLLATE NOCASE",
                ("documents", f"%{ext}"),
            )
        # Drop duplicate rows and renumber the affected vaults (see
        # dedupe_files). Idempotent: once the vault is clean this is a
        # no-op, so it is safe to run on every open.
        self.dedupe_files()

    def find_by_telegram_file_id(
        self, telegram_file_id: str, *, user_id: int
    ) -> FileRecord | None:
        """The vault record for a Telegram file_id in this user's vault,
        or None if the user does not have it yet.

        The match is per-user: the same file in another user's vault is
        not a duplicate. An empty file_id never matches — legacy
        on-disk records have no file_id, and two of them must never
        count as duplicates of each other. The earliest row wins if a
        duplicate somehow exists (the migration removes those, but a
        stale copy should still point at the original).
        """
        if not telegram_file_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM files WHERE user_id = ? AND telegram_file_id = ?"
            " ORDER BY id ASC LIMIT 1",
            (user_id, telegram_file_id),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def dedupe_files(self) -> dict:
        """Remove duplicate file rows and renumber the affected vaults.

        A duplicate is a second (or later) row for the SAME user with
        the SAME non-empty telegram_file_id — the same file sent twice
        (e.g. re-sending RDR.mp3 stored it again as a new number).
        The earliest row (lowest id) is kept; the rest are deleted
        (any on-disk copy of a dropped row is deleted too, though
        file_id rows are normally stored by reference and have no
        disk copy). Each affected user's files are then renumbered
        1..N in id order, so the vault reads gaplessly again and
        /get, /delete, /list, /search and /stats all agree.

        Legacy on-disk records (empty telegram_file_id) are never
        treated as duplicates. Idempotent: once cleaned, a later run
        finds nothing to do. Returns {"removed": <rows deleted>,
        "users": [user ids whose vaults were renumbered]}.
        """
        groups = self._conn.execute(
            "SELECT user_id, telegram_file_id, COUNT(*) AS c FROM files"
            " WHERE telegram_file_id != ''"
            " GROUP BY user_id, telegram_file_id HAVING c > 1"
        ).fetchall()
        if not groups:
            return {"removed": 0, "users": []}
        affected: set[int] = set()
        removed = 0
        for user_id, file_id, _count in groups:
            ids = [
                row[0]
                for row in self._conn.execute(
                    "SELECT id FROM files WHERE user_id = ?"
                    " AND telegram_file_id = ? ORDER BY id ASC",
                    (user_id, file_id),
                ).fetchall()
            ]
            _keep, *drop = ids
            affected.add(user_id)
            for row_id in drop:
                rec = self._get_by_row_id(row_id, user_id=user_id)
                if rec is not None and rec.stored_name:
                    try:
                        rec.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._conn.execute(
                    "DELETE FROM files WHERE id = ?", (row_id,)
                )
                removed += 1
        for user_id in affected:
            self._conn.execute(
                "UPDATE files SET user_num = ("
                "  SELECT COUNT(*) FROM files f2"
                "  WHERE f2.user_id = files.user_id AND f2.id <= files.id"
                ") WHERE user_id = ?",
                (user_id,),
            )
        self._conn.commit()
        return {"removed": removed, "users": sorted(affected)}

    # -- internal helpers -------------------------------------------------

    def _row_to_record(self, row: sqlite3.Row) -> FileRecord:
        keys = row.keys()
        rec = FileRecord(
            id=row["id"],
            user_id=row["user_id"] if "user_id" in keys and row["user_id"] is not None else 0,
            filename=row["filename"],
            stored_name=row["stored_name"],
            category=row["category"],
            file_type=row["file_type"],
            size=row["size"],
            file_id=row["file_id"],
            caption=row["caption"],
            created_at=row["created_at"],
            user_num=row["user_num"] if "user_num" in keys and row["user_num"] else 0,
            telegram_file_id=row["telegram_file_id"] if "telegram_file_id" in keys else "",
            source_chat_id=row["source_chat_id"] if "source_chat_id" in keys else None,
            source_message_id=row["source_message_id"] if "source_message_id" in keys else None,
        )
        rec._path = self.data_dir / rec.category / rec.stored_name  # type: ignore[attr-defined]
        return rec

    def _row_to_note(self, row: sqlite3.Row) -> NoteRecord:
        keys = row.keys()
        return NoteRecord(
            id=row["id"],
            user_id=row["user_id"] if "user_id" in keys and row["user_id"] is not None else 0,
            text=row["text"],
            created_at=row["created_at"],
        )

    def _row_to_link(self, row: sqlite3.Row) -> LinkRecord:
        keys = row.keys()
        return LinkRecord(
            id=row["id"],
            user_id=row["user_id"] if "user_id" in keys and row["user_id"] is not None else 0,
            url=row["url"],
            title=row["title"] if "title" in keys else "",
            created_at=row["created_at"],
        )

    # -- files ------------------------------------------------------------

    def add_file(
        self,
        *,
        user_id: int,
        file_id: str,
        filename: str,
        file_type: str,
        size: int = 0,
        caption: str = "",
        data: bytes | None = None,
        source_path: str | Path | None = None,
        telegram_file_id: str = "",
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
    ) -> FileRecord:
        """Index a file in the requesting user's vault and return the
        created FileRecord.

        Two kinds of record:
        * By reference (the normal case now): pass `telegram_file_id` and
          the source message (`source_chat_id`, `source_message_id`) and
          no `data`/`source_path`. Nothing is written to disk — Telegram
          holds the bytes; the vault keeps the reference.
        * On disk (legacy): pass `data=` or `source_path=` and the file is
          written under <data_dir>/<category>/, as before. Old records
          keep working this way.
        The stored filename is prefixed with the row id, so two uploads
        with the same name never collide. The file's user-facing number
        is the next per-user sequence number for this user only
        (max user_num for this user_id + 1) — it is never reused after a
        delete, and one user's numbers never affect another's.
        """
        clean = safe_filename(filename)
        category = classify_category(clean, file_type)
        now = int(time.time())
        user_num = self._conn.execute(
            "SELECT COALESCE(MAX(user_num), 0) FROM files WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0] + 1
        cur = self._conn.execute(
            "INSERT INTO files (user_id, user_num, filename, stored_name,"
            " category, file_type, size, file_id, caption, created_at,"
            " telegram_file_id, source_chat_id, source_message_id)"
            " VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, user_num, clean, category, file_type, int(size),
             file_id, caption or "", now, telegram_file_id or "",
             source_chat_id, source_message_id),
        )
        row_id = cur.lastrowid
        if data is not None or source_path is not None:
            stored_name = f"{row_id}_{clean}"
            dest = self.data_dir / category / stored_name
            if data is not None:
                dest.write_bytes(data)
            else:
                shutil.move(str(source_path), dest)
            self._conn.execute(
                "UPDATE files SET stored_name = ? WHERE id = ?",
                (stored_name, row_id),
            )
        self._conn.commit()
        return self._get_by_row_id(row_id, user_id=user_id)  # type: ignore[arg-type]

    def _get_by_row_id(self, row_id: int, *, user_id: int) -> FileRecord | None:
        """Fetch one file by its internal database row id. Internal use
        only (e.g. returning the record just inserted) — the row id is
        never shown to a user; user-facing lookups use get_file, which
        takes the per-user number."""
        if not _id_in_range(row_id):
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM files WHERE id = ? AND user_id = ?",
                (row_id, user_id),
            ).fetchone()
        except OverflowError:
            return None
        return self._row_to_record(row) if row else None

    def get_file(self, number: int, *, user_id: int) -> FileRecord | None:
        """Fetch one file by its PER-USER number — only if it belongs to
        `user_id`. Returns None for another user's file (indistinguishable
        from a file that does not exist) and for a number that cannot be
        a SQLite integer (an oversized number is 'not found', never a
        crash). The global database id is never a valid lookup here."""
        if not _id_in_range(number):
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM files WHERE user_num = ? AND user_id = ?",
                (number, user_id),
            ).fetchone()
        except OverflowError:
            return None
        return self._row_to_record(row) if row else None

    def resolve(self, query: str, *, user_id: int) -> list[FileRecord]:
        """Find files by per-user number (if the query is numeric) or by
        name/caption, within the requesting user's vault only.

        Returns a list: 0 = not found, 1 = unique match, >1 = ambiguous.
        """
        query = (query or "").strip()
        if not query:
            return []
        if query.isdigit():
            try:
                num = int(query)
            except ValueError:
                # A string of thousands of digits exceeds Python's int
                # conversion limit — it is simply not a file id.
                return []
            if not _id_in_range(num):
                return []
            rec = self.get_file(num, user_id=user_id)
            return [rec] if rec else []
        like = _like_pattern(query)
        rows = self._conn.execute(
            "SELECT * FROM files WHERE user_id = ? AND (filename COLLATE "
            "NOCASE LIKE ? ESCAPE '\\' OR caption COLLATE NOCASE LIKE ?"
            " ESCAPE '\\') ORDER BY created_at DESC",
            (user_id, like, like),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_files(self, limit: int = 10, offset: int = 0,
                   *, user_id: int,
                   category: str | None = None,
                   extensions: tuple[str, ...] | None = None
                   ) -> tuple[list[FileRecord], int]:
        """Newest first, within the requesting user's vault only.
        Returns (records, total_count).

        Optional filters: `category` (one of the on-disk categories, e.g.
        "photos") and `extensions` (filename suffixes, e.g. (".apk", ".zip"),
        matched case-insensitively). Both filters narrow the count too, so
        paging stays correct.
        """
        where: list[str] = ["user_id = ?"]
        params: list = [user_id]
        if category:
            where.append("category = ?")
            params.append(category)
        if extensions:
            where.append(
                "("
                + " OR ".join(
                    "filename LIKE ? COLLATE NOCASE" for _ in extensions
                )
                + ")"
            )
            params.extend(f"%{ext}" for ext in extensions)
        where_sql = "WHERE " + " AND ".join(where)
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM files {where_sql}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"SELECT * FROM files {where_sql}"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [self._row_to_record(r) for r in rows], total

    def search_files(self, query: str, *, user_id: int,
                     limit: int = 20) -> list[FileRecord]:
        like = _like_pattern(query)
        rows = self._conn.execute(
            "SELECT * FROM files WHERE user_id = ? AND (filename COLLATE "
            "NOCASE LIKE ? ESCAPE '\\' OR caption COLLATE NOCASE LIKE ?"
            " ESCAPE '\\') ORDER BY created_at DESC LIMIT ?",
            (user_id, like, like, limit),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def delete_file(self, number: int, *, user_id: int) -> FileRecord | None:
        """Remove a file — identified by its PER-USER number — from the
        requesting user's index (and from disk if it was stored there).
        Returns the record, or None if the file does not belong to this
        user. The DELETE itself is scoped to the user (see below), so a
        number belonging to another user can never remove their file."""
        rec = self.get_file(number, user_id=user_id)
        if rec is None:
            return None
        if rec.stored_name:
            try:
                rec.path.unlink(missing_ok=True)
            except OSError:
                pass
        # Defense-in-depth: the DELETE uses the internal row id AND the
        # user id, not just the lookup above.
        self._conn.execute(
            "DELETE FROM files WHERE id = ? AND user_id = ?",
            (rec.id, user_id),
        )
        self._conn.commit()
        return rec

    def clear_all(self, *, user_id: int) -> tuple[int, int]:
        """Delete ALL of the requesting user's files (disk + index) and
        ALL of their notes AND links. Other users' vaults are untouched.

        Returns (n_files_deleted, n_notes_deleted). Only the bot's /clear
        command (after its confirmation tap) calls this; it counts the
        links separately before calling, so the confirmation message can
        report them too."""
        records, n_files = self.list_files(limit=1_000_000, offset=0,
                                           user_id=user_id)
        for rec in records:
            if rec.stored_name:
                try:
                    rec.path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._conn.execute("DELETE FROM files WHERE user_id = ?", (user_id,))
        n_notes = self._conn.execute(
            "SELECT COUNT(*) FROM notes WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        self._conn.execute("DELETE FROM notes WHERE user_id = ?", (user_id,))
        self._conn.execute("DELETE FROM links WHERE user_id = ?", (user_id,))
        self._conn.commit()
        return n_files, n_notes

    def clear_files(self, *, user_id: int) -> tuple[int, int]:
        """Delete ALL of the requesting user's FILES only — notes, links
        and reminders are untouched. Other users' vaults are untouched.

        Returns (n_files_deleted, total_bytes_deleted). Only the bot's
        /start fresh-start choice (after its confirmation tap) calls
        this; the full wipe lives in clear_all() and is used by /clear.
        Because the next file number is MAX+1 over the user's remaining
        rows, wiping the files also restarts numbering from #1.
        """
        records, n_files = self.list_files(limit=1_000_000, offset=0,
                                           user_id=user_id)
        total_size = 0
        for rec in records:
            total_size += rec.size or 0
            if rec.stored_name:
                try:
                    rec.path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._conn.execute("DELETE FROM files WHERE user_id = ?", (user_id,))
        self._conn.commit()
        return n_files, total_size

    def stats(self, *, user_id: int) -> dict:
        """Totals for the requesting user's vault only: file count,
        total bytes, and a per-category breakdown."""
        total, total_size = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files"
            " WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        by_category = {}
        for category, count, size in self._conn.execute(
            "SELECT category, COUNT(*), COALESCE(SUM(size), 0)"
            " FROM files WHERE user_id = ? GROUP BY category ORDER BY category",
            (user_id,),
        ).fetchall():
            by_category[category] = {"count": count, "size": size}
        return {
            "total_files": total,
            "total_size": total_size,
            "by_category": by_category,
        }

    def quota_check(self, user_id: int, size: int) -> str | None:
        """Check the per-user limits before storing one more file of
        `size` bytes. Returns a polite refusal message if any limit
        would be exceeded, else None (the store may proceed)."""
        count, total, today = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0),"
            " COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0)"
            " FROM files WHERE user_id = ?",
            (int(time.time()) - 24 * 60 * 60, user_id),
        ).fetchone()
        if count >= MAX_FILES_PER_USER:
            return (
                f"Your vault already holds {MAX_FILES_PER_USER} files — the "
                "limit per user. Delete some with /delete to make room."
            )
        if total + size > MAX_BYTES_PER_USER:
            return (
                "That file would take your vault over the "
                f"{human_size(MAX_BYTES_PER_USER)} limit per user "
                f"(you are at {human_size(total)}). Delete some files first."
            )
        if today >= MAX_FILES_PER_DAY:
            return (
                f"You have stored {MAX_FILES_PER_DAY} files in the last day — "
                "the daily limit per user. Try again tomorrow."
            )
        return None

    # -- notes ------------------------------------------------------------

    def add_note(self, text: str, *, user_id: int) -> NoteRecord:
        text = (text or "").strip()
        if not text:
            raise ValueError("Note text is empty.")
        count = self._conn.execute(
            "SELECT COUNT(*) FROM notes WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        if count >= MAX_NOTES_PER_USER:
            raise ValueError(
                f"You already have {MAX_NOTES_PER_USER} notes — the limit "
                "per user. Delete some with /delnote to make room."
            )
        cur = self._conn.execute(
            "INSERT INTO notes (user_id, text, created_at) VALUES (?, ?, ?)",
            (user_id, text, int(time.time())),
        )
        self._conn.commit()
        return self.get_note(cur.lastrowid, user_id=user_id)  # type: ignore[arg-type]

    def get_note(self, note_id: int, *, user_id: int) -> NoteRecord | None:
        if not _id_in_range(note_id):
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM notes WHERE id = ? AND user_id = ?",
                (note_id, user_id),
            ).fetchone()
        except OverflowError:
            return None
        return self._row_to_note(row) if row else None

    def list_notes(self, limit: int = 20, offset: int = 0,
                   *, user_id: int) -> tuple[list[NoteRecord], int]:
        total = self._conn.execute(
            "SELECT COUNT(*) FROM notes WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        rows = self._conn.execute(
            "SELECT * FROM notes WHERE user_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
        return ([self._row_to_note(r) for r in rows], total)

    def search_notes(self, query: str, *, user_id: int,
                     limit: int = 20) -> list[NoteRecord]:
        like = _like_pattern(query)
        rows = self._conn.execute(
            "SELECT * FROM notes WHERE user_id = ? AND text COLLATE NOCASE"
            " LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT ?",
            (user_id, like, limit),
        ).fetchall()
        return [self._row_to_note(r) for r in rows]

    def delete_note(self, note_id: int, *, user_id: int) -> NoteRecord | None:
        rec = self.get_note(note_id, user_id=user_id)
        if rec is None:
            return None
        # Defense-in-depth: the DELETE itself is scoped to the user.
        self._conn.execute(
            "DELETE FROM notes WHERE id = ? AND user_id = ?",
            (note_id, user_id),
        )
        self._conn.commit()
        return rec

    # -- links ------------------------------------------------------------
    # Saved links, scoped exactly like notes: every method takes a
    # required keyword-only user_id and only ever sees that user's rows.

    # A URL is a scheme (optional — "example.com/x" gets https:// added
    # by the caller) plus a dotted host, plus an optional path/query.
    # No spaces, no other schemes (no javascript:, no file:).
    _URL_RE = re.compile(
        r"^(?:https?://)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
        r"(?::\d{1,5})?(?:/[^\s]*)?$"
    )

    @staticmethod
    def normalise_url(raw: str) -> str | None:
        """Clean a user-supplied URL, or None if it is not a URL at all.
        Adds https:// when the scheme is missing; refuses anything with
        spaces, an unknown scheme, or no dotted host. Returns the URL
        with any trailing punctuation (.,;:!?) stripped."""
        url = (raw or "").strip().rstrip(".,;:!?)]>")
        if not url or len(url) > 2048 or any(ch.isspace() for ch in url):
            return None
        if not Store._URL_RE.match(url):
            return None
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        return url

    def add_link(self, url: str, title: str = "", *, user_id: int) -> LinkRecord:
        clean = self.normalise_url(url)
        if clean is None:
            raise ValueError(
                "That doesn't look like a link — send a full URL "
                "(https://example.com/…) or a domain (example.com)."
            )
        count = self._conn.execute(
            "SELECT COUNT(*) FROM links WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        if count >= MAX_LINKS_PER_USER:
            raise ValueError(
                f"You already have {MAX_LINKS_PER_USER} links — the limit "
                "per user. Delete some with /dellink to make room."
            )
        cur = self._conn.execute(
            "INSERT INTO links (user_id, url, title, created_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, clean, (title or "").strip()[:200], int(time.time())),
        )
        self._conn.commit()
        return self.get_link(cur.lastrowid, user_id=user_id)  # type: ignore[arg-type]

    def get_link(self, link_id: int, *, user_id: int) -> LinkRecord | None:
        if not _id_in_range(link_id):
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM links WHERE id = ? AND user_id = ?",
                (link_id, user_id),
            ).fetchone()
        except OverflowError:
            return None
        return self._row_to_link(row) if row else None

    def list_links(self, limit: int = 10, offset: int = 0,
                   *, user_id: int) -> tuple[list[LinkRecord], int]:
        total = self._conn.execute(
            "SELECT COUNT(*) FROM links WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        rows = self._conn.execute(
            "SELECT * FROM links WHERE user_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
        return ([self._row_to_link(r) for r in rows], total)

    def search_links(self, query: str, *, user_id: int,
                     limit: int = 20) -> list[LinkRecord]:
        like = _like_pattern(query)
        rows = self._conn.execute(
            "SELECT * FROM links WHERE user_id = ? AND (url COLLATE NOCASE"
            " LIKE ? ESCAPE '\\' OR title COLLATE NOCASE LIKE ?"
            " ESCAPE '\\') ORDER BY created_at DESC LIMIT ?",
            (user_id, like, like, limit),
        ).fetchall()
        return [self._row_to_link(r) for r in rows]

    def delete_link(self, link_id: int, *, user_id: int) -> LinkRecord | None:
        rec = self.get_link(link_id, user_id=user_id)
        if rec is None:
            return None
        # Defense-in-depth: the DELETE itself is scoped to the user.
        self._conn.execute(
            "DELETE FROM links WHERE id = ? AND user_id = ?",
            (link_id, user_id),
        )
        self._conn.commit()
        return rec

    # -- users ------------------------------------------------------------
    # One row per Telegram user id. A row is created the first time the
    # user talks to the bot (INSERT OR IGNORE). api_key holds the user's
    # OWN Gemini key, Fernet-encrypted (see keystore.py); an empty value
    # means no key linked. is_banned blocks the user from every command.

    def ensure_user(self, user_id: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO users (user_id, created_at)"
            " VALUES (?, ?)",
            (user_id, int(time.time())),
        )
        self._conn.commit()

    def touch_user(self, user_id: int) -> None:
        """Mark the user as active right now. Stores a timestamp
        ONLY — never any message content, never what they did. Called
        on every interaction that reaches a handler (commands, plain
        chat, file saves, button taps, admin commands). Updates an
        EXISTING row only: the row is created by ensure_user on first
        contact (require_user / callbacks), so touching never conjures
        a user — a first-ever /admin must not change /admin's count."""
        self._conn.execute(
            "UPDATE users SET last_seen = ? WHERE user_id = ?",
            (int(time.time()), user_id),
        )
        self._conn.commit()

    def get_user(self, user_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT user_id, api_key, is_banned, created_at, last_seen"
            " FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "user_id": row["user_id"],
            "has_api_key": bool(row["api_key"]),
            "is_banned": bool(row["is_banned"]),
            "created_at": row["created_at"],
            "last_seen": row["last_seen"],
        }

    def is_banned(self, user_id: int) -> bool:
        row = self._conn.execute(
            "SELECT is_banned FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row and row[0])

    def set_banned(self, user_id: int, banned: bool) -> None:
        self.ensure_user(user_id)
        self._conn.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?",
            (1 if banned else 0, user_id),
        )
        self._conn.commit()

    def set_user_key(self, user_id: int, api_key: str) -> None:
        """Store the user's own Gemini key, encrypted at rest. The key
        value is never logged."""
        self.ensure_user(user_id)
        token = self.key_vault.encrypt(api_key)
        self._conn.execute(
            "UPDATE users SET api_key = ? WHERE user_id = ?",
            (token, user_id),
        )
        self._conn.commit()

    def get_user_key(self, user_id: int) -> str | None:
        """Return the user's own Gemini key (decrypted), or None if they
        never linked one or the stored token no longer decrypts."""
        row = self._conn.execute(
            "SELECT api_key FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None or not row[0]:
            return None
        return self.key_vault.decrypt(row[0])

    def delete_user_key(self, user_id: int) -> bool:
        """Remove the user's linked key. Returns True if one was stored."""
        row = self._conn.execute(
            "SELECT api_key FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        had_key = bool(row and row[0])
        self._conn.execute(
            "UPDATE users SET api_key = '' WHERE user_id = ?", (user_id,)
        )
        self._conn.commit()
        return had_key

    def count_users(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list_users(self) -> list[dict]:
        """One row per user, oldest first, for the owner's /users
        command: id, banned flag, first-seen and last-seen timestamps,
        and file/note/link counts. No names are stored anywhere — the
        bot resolves those live from Telegram at display time."""
        rows = self._conn.execute(
            "SELECT u.user_id, u.is_banned, u.created_at, u.last_seen,"
            " (SELECT COUNT(*) FROM files f WHERE f.user_id = u.user_id),"
            " (SELECT COUNT(*) FROM notes n WHERE n.user_id = u.user_id),"
            " (SELECT COUNT(*) FROM links l WHERE l.user_id = u.user_id)"
            " FROM users u ORDER BY u.created_at ASC"
        ).fetchall()
        return [
            {
                "user_id": row[0],
                "is_banned": bool(row[1]),
                "created_at": row[2],
                "last_seen": row[3],
                "files": row[4],
                "notes": row[5],
                "links": row[6],
            }
            for row in rows
        ]

    def aggregate_stats(self) -> dict:
        """Aggregate numbers only — no names, contents, or per-user data.
        For the owner's /admin command."""
        total_users = self._conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]
        total_files, total_size = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files"
        ).fetchone()
        total_notes = self._conn.execute(
            "SELECT COUNT(*) FROM notes"
        ).fetchone()[0]
        total_links = self._conn.execute(
            "SELECT COUNT(*) FROM links"
        ).fetchone()[0]
        banned = self._conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_banned = 1"
        ).fetchone()[0]
        return {
            "total_users": total_users,
            "total_files": total_files,
            "total_size": total_size,
            "total_notes": total_notes,
            "total_links": total_links,
            "banned_users": banned,
        }

    def close(self) -> None:
        self._conn.close()
