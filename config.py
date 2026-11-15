"""Sanctum Sanctorum — configuration.

Everything secret or deployment-specific comes from environment variables.
Nothing sensitive is ever hardcoded here.

Required:
    TELEGRAM_BOT_TOKEN  Bot token from @BotFather on Telegram.
    OWNER_ID            The bot operator's Telegram user id (an integer).
                        The owner is an admin (/admin, /ban, /unban) and
                        the owner's Gemini key is used ONLY for the owner.
                        Everyone else gets their own private vault and
                        must link their own Gemini key for /chat.

Optional:
    SANCTUM_DATA_DIR    Where files + the SQLite index live.
                        Default: ./data next to this file.
    GEMINI_API_KEY      The OWNER's free Google AI Studio key for /chat
                        (https://aistudio.google.com). It is NEVER used
                        for any other user — other users must link their
                        own key with /setkey. Without it the owner still
                        talks in character with the local assistant.
    SANCTUM_KEY_SECRET  Server secret used to encrypt users' linked
                        Gemini keys at rest (see keystore.py). Optional:
                        if it is not set, a random secret is generated
                        once and kept in <data_dir>/key_secret.
"""

from __future__ import annotations

import os
from pathlib import Path

BOT_TOKEN: str | None = os.environ.get("TELEGRAM_BOT_TOKEN") or None

_owner_raw = os.environ.get("OWNER_ID")
try:
    OWNER_ID: int | None = int(_owner_raw) if _owner_raw else None
except ValueError:
    OWNER_ID = None

DATA_DIR: Path = Path(
    os.environ.get("SANCTUM_DATA_DIR", Path(__file__).resolve().parent / "data")
)
DB_PATH: Path = DATA_DIR / "vault.db"

# Optional. The OWNER's free key from Google AI Studio
# (https://aistudio.google.com). Only ever used for the owner's /chat —
# never for any other user. Empty/missing means no AI for the owner
# either; the bot still talks in character with its local assistant.
GEMINI_API_KEY: str | None = os.environ.get("GEMINI_API_KEY") or None

# Optional. Server secret for encrypting users' linked Gemini keys at
# rest (see keystore.py). If unset, a random secret is generated once
# and kept in <data_dir>/key_secret.
KEY_SECRET: str | None = os.environ.get("SANCTUM_KEY_SECRET") or None

# Telegram Bot API limit for SENDING a file through a bot (50 MB).
# Files are stored by file_id now, so nothing is downloaded — the old
# 20 MB download limit no longer applies. Files over this size are
# sent back by forwarding the original message instead.
MAX_SEND_BYTES = 50 * 1024 * 1024  # 50 MB

# Telegram Bot API limit for DOWNLOADING a file with getFile (20 MB).
# /analyse downloads the file to read it, so files over this
# size are refused politely instead of failing mid-download.
MAX_ANALYSE_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB

# How many files / notes to show per page in /list and /notes.
PAGE_SIZE = 10


def validate() -> None:
    """Fail fast with a clear message if required config is missing."""
    missing = []
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if OWNER_ID is None:
        missing.append("OWNER_ID (must be your numeric Telegram user id)")
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill it in, or export them."
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
