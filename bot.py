"""Sanctum Sanctorum — Telegram bot: personal file vault + local assistant.

PUBLIC multi-user bot: anyone may use it, and every user gets a private
vault of their own. Every file, note, and reminder belongs to exactly
one Telegram user id, and every query is filtered by that id — a user
can never see another user's vault. The owner (OWNER_ID) is an admin
(/admin, /users, /ban, /unban) but has no access to other users' vaults.

The AI is optional: /chat is the keeper's alone, and /analyse is
bring-your-own-key — the owner's Gemini key is used ONLY for the
owner. Any other user who wants /analyse must link their own free
Gemini key with /setkey; without one, it tells them how to get one,
and the vault works fully without AI.

Runs on long polling, so it works anywhere (Koyeb, a VM, a laptop).

Setup:
    1. Talk to @BotFather on Telegram, /newbot, copy the token.
    2. Copy .env.example to .env and fill in TELEGRAM_BOT_TOKEN and OWNER_ID.
       (Get your own id from @userinfobot.)
    3. pip install -r requirements.txt
    4. python bot.py
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from ai import (
    MAX_ANALYSE_TEXT_CHARS,
    analyze_text_with_ai,
    chat_with_ai,
)
from assistant import answer_ask, answer_chat, reminder_callback, schedule_reminder  # noqa: F401
from vault import (
    APP_ARCHIVE_EXTENSIONS,
    CATEGORY_LABELS,
    MAX_BYTES_PER_USER,
    MAX_FILES_PER_DAY,
    MAX_FILES_PER_USER,
    MAX_LINKS_PER_USER,
    MAX_NOTES_PER_USER,
    MAX_REMINDERS_PER_USER,
    FileRecord,
    LinkRecord,
    Store,
    human_size,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("sanctum")

STORE: Store | None = None


def store() -> Store:
    assert STORE is not None
    return STORE


def is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id == config.OWNER_ID


def _user_id(update: Update) -> int | None:
    user = update.effective_user
    return user.id if user else None


def _is_private_chat(update: Update) -> bool:
    """True only when the update comes from a private chat with the bot.
    The bot must operate ONLY in private chats: vault output, search
    results, and /clear confirmations must never be visible to group
    members, and a group member must never be able to tap someone
    else's confirmation button. `effective_chat.type` is always present
    on real updates; the getattr default keeps old test doubles (which
    have no .type) behaving as private."""
    chat = update.effective_chat
    if chat is None:
        return True
    return getattr(chat, "type", "private") == "private"


def _parse_id_arg(arg: str) -> int | None:
    """Parse a numeric id argument, or None if it is not a plain integer
    that fits in a signed 64-bit SQLite integer. A huge id (e.g. 40
    digits) must be a polite refusal, never an uncaught OverflowError
    from SQLite or a ValueError from int() on a 5000-digit string."""
    if not arg or not arg.isdigit():
        return None
    try:
        value = int(arg)
    except ValueError:
        return None
    if not -(2**63) <= value <= 2**63 - 1:
        return None
    return value


def require_user(func):
    """Decorator: any Telegram user may use the bot, unless banned.

    Creates the user's row on first contact (so /admin can count
    users). A banned user gets a polite refusal on every command.
    The bot works ONLY in private chats — group use is refused before
    any user row is created and before anything is shown.
    """

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = _user_id(update)
        if uid is None:
            return None
        if not _is_private_chat(update):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "The Sanctum only works in a private chat with me — "
                    "please talk to me directly, not in a group."
                )
            return None
        store().ensure_user(uid)
        if store().is_banned(uid):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "The Sanctum is closed to you for now. If you think "
                    "this is a mistake, contact the keeper of this bot."
                )
            return None
        # The user is in: record the interaction (timestamp only —
        # last_seen never stores what they said or sent).
        store().touch_user(uid)
        return await func(update, context)

    return wrapper


def owner_only(func):
    """Decorator: only the configured owner (the bot admin). Also
    private-chat only, so /admin totals can never leak to a group."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_private_chat(update):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "The Sanctum only works in a private chat with me — "
                    "please talk to me directly, not in a group."
                )
            return None
        uid = _user_id(update)
        if not is_owner(uid):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "That command is for the keeper of this bot only."
                )
            return None
        # The keeper's own interaction counts too.
        store().touch_user(uid)
        return await func(update, context)

    return wrapper


def _callback_user_id(update: Update) -> int | None:
    """User id for an inline-button tap, or None if it is banned/missing.
    Edits the message with a refusal for a banned user."""
    query = update.callback_query
    user = query.from_user if query else None
    uid = user.id if user else None
    return uid


async def _callback_allowed(update: Update) -> int | None:
    """For callback handlers: return the tapping user's id, or None
    (after answering/refusing) when the tap must not proceed. Refuses
    taps from non-private chats, so a group member can never page
    through or confirm-wipe a vault from a group message."""
    query = update.callback_query
    await query.answer()
    if not _is_private_chat(update):
        try:
            await query.edit_message_text(
                "The Sanctum only works in a private chat with me."
            )
        except Exception:  # noqa: BLE001 - best effort only
            pass
        return None
    uid = _callback_user_id(update)
    if uid is None:
        return None
    store().ensure_user(uid)
    if store().is_banned(uid):
        await query.edit_message_text(
            "The Sanctum is closed to you for now."
        )
        return None
    # A button tap is activity too.
    store().touch_user(uid)
    return uid


# ---------------------------------------------------------------------------
# /start and /help
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🏛️ *Sanctum Sanctorum* — your private Telegram vault.\n\n"
    "Send me any file and I keep it safe, in a vault only you can open. "
    "Files are kept as Telegram references, not downloaded to this server "
    "— Telegram holds the bytes, the vault only keeps the file's ID, name, "
    "size, and the date you sent it. Files up to 50 MB come straight back; "
    "larger ones come back by forwarding the original message. If that "
    "original message is gone (chat cleared), a file over 50 MB can't be "
    "retrieved at all. Every user "
    "has their own vault — nobody else can see yours.\n\n"
    f"Limits per user: {MAX_FILES_PER_USER} files, "
    f"{human_size(MAX_BYTES_PER_USER)} total, "
    f"{MAX_FILES_PER_DAY} new files per day, "
    f"{MAX_NOTES_PER_USER} notes, "
    f"{MAX_LINKS_PER_USER} links, "
    f"{MAX_REMINDERS_PER_USER} active reminders.\n\n"
    "*Essentials — in the Menu*\n"
    "/start — open the Sanctum\n"
    "/help — show this list\n"
    "/files — browse your vault (filter buttons: All, Images, Videos, Audio, Docs, Apps)\n"
    "/search <text> — search by filename or caption\n"
    "/get <number or name> — download a file back\n"
    "/stats — how much you have stored\n"
    "/note <text> — save a note · /note <id> — read one back\n"
    "/links <url> [title] — save a link · send a bare link alone and I save it\n"
    "/analyse <file number or name> — AI summary of a vault file (text files and PDFs only)\n\n"
    "*More — type to use*\n"
    "/list /images /videos /audio /docs /apps — other ways to browse your vault\n"
    "/delete <number or name> — delete a file · /clear — wipe your vault (asks first)\n"
    "/notes — list your notes · /delnote <id> — delete a note\n"
    "/dellink <id> — delete a saved link\n"
    "/remind <minutes> <text> — set a reminder\n"
    "/ask <question> — ask about the vault, notes, arithmetic, date/time\n"
    "/setkey <key> — link your own free Gemini key · /delkey — remove it · /mykey — check it (never shown back)\n\n"
    "Your files are numbered from #1 inside YOUR vault only — the "
    "numbers you see are yours alone, and /get and /delete take your "
    "own numbers. Sending a file you already have just points you "
    "back to its original number — no duplicates.\n\n"
    "You can also just type to me — I answer greetings, jokes, and simple "
    "chat in character, no command needed.\n\n"
    "Tip: give a file a caption when you send it — the caption is searchable."
)

# Shown only to the keeper, appended to HELP_TEXT by help_cmd: other
# users never see the admin commands — not in the Menu, not in /help.
ADMIN_HELP_TEXT = (
    "*Admin (keeper only)*\n"
    "/admin — totals across all users\n"
    "/users — list every user (name, ID, first seen, last seen, counts)\n"
    "/ban <user_id> — ban a user\n"
    "/unban <user_id> — unban a user\n\n"
)

# Shown only to the keeper, appended to HELP_TEXT by help_cmd: /chat
# is the keeper's alone (Nov 15, 2026) — other users never see it,
# not in the Menu, not in /help.
KEEPER_HELP_TEXT = (
    "*Keeper's AI*\n"
    "/chat <message> — talk with the Sanctum AI, on the keeper's own Gemini key\n\n"
)


WELCOME_TEXT = (
    "🏛️ Welcome to your Sanctum Sanctorum.\n\n"
    "Send me any file — document, photo, video, audio — and I will "
    "store it in your private vault. Only you can open it. "
    "Use /help for every command.\n\n"
    "Files up to 50 MB come straight back. Bigger ones I return by "
    "forwarding the original message — so if you clear this chat, a "
    "file over 50 MB can't come back."
)


@require_user
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _user_id(update)
    stats = store().stats(user_id=uid)
    if stats["total_files"] == 0:
        await update.message.reply_text(
            WELCOME_TEXT, parse_mode="Markdown",
        )
        return
    # A returning user with files gets a choice, not a silent welcome:
    # they may have cleared the chat and want a clean slate. The bot
    # cannot detect a clear, so it asks instead of guessing — "start
    # fresh" wipes their files only (notes, links, reminders stay),
    # after a second confirmation tap.
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Keep my files", callback_data="start:keep")],
        [InlineKeyboardButton("Start fresh from #1",
                              callback_data="start:fresh")],
    ])
    await update.message.reply_text(
        "🏛️ Welcome back to your Sanctum Sanctorum.\n\n"
        f"Your vault still has {stats['total_files']} file(s) "
        f"({human_size(stats['total_size'])}).\n\n"
        "Keep them, or start fresh from #1?",
        reply_markup=keyboard,
    )


async def start_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start return-visit choice ("start:keep" / "start:fresh")."""
    uid = await _callback_allowed(update)
    if uid is None:
        return
    query = update.callback_query
    action = query.data.split(":", 1)[1] if ":" in query.data else ""
    if action == "keep":
        await query.edit_message_text(WELCOME_TEXT, parse_mode="Markdown")
        return
    # "fresh": show the files-only wipe confirmation, with the count.
    stats = store().stats(user_id=uid)
    if stats["total_files"] == 0:
        await query.edit_message_text(WELCOME_TEXT, parse_mode="Markdown")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete my files",
                              callback_data="fresh:yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="fresh:no")],
    ])
    await query.edit_message_text(
        f"⚠️ This will permanently delete {stats['total_files']} file(s) "
        f"({human_size(stats['total_size'])}) from your vault. Your notes, "
        "links and reminders stay. This cannot be undone.\n\n"
        "Start fresh from #1?",
        reply_markup=keyboard,
    )


async def fresh_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the fresh-start confirmation ("fresh:yes" / "fresh:no")."""
    uid = await _callback_allowed(update)
    if uid is None:
        return
    query = update.callback_query
    action = query.data.split(":", 1)[1] if ":" in query.data else ""
    if action == "yes":
        n_files, _size = store().clear_files(user_id=uid)
        await query.edit_message_text(
            f"🗑️ Vault cleared: {n_files} file(s) deleted. "
            "Your next file will be #1."
        )
    else:
        await query.edit_message_text(
            "Kept everything — your vault is untouched."
        )


@require_user
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = HELP_TEXT
    if is_owner(_user_id(update)):
        # The keeper sees the keeper-only sections; other users never do.
        text = text + "\n" + KEEPER_HELP_TEXT + ADMIN_HELP_TEXT
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Vault commands (all scoped to the requesting user's vault)
# ---------------------------------------------------------------------------

def _list_text(records: list[FileRecord], page: int, total: int,
               title: str = "Your files", emoji: str = "📁") -> str:
    if total == 0:
        if title == "Your files":
            return "Your vault is empty. Send me any file and I will keep it safe."
        return f"No {title.lower()} in your vault yet."
    pages = max(1, (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE)
    lines = [f"{emoji} {title} — page {page + 1} of {pages} ({total} total):"]
    lines += [f"• {r.display()}" for r in records]
    lines.append("\n/get <id or name> to download one.")
    return "\n".join(lines)


# Filter buttons shown above every file list (/list and every category
# view). Tapping one switches the view through the existing
# "cat:<key>:<page>" callback scheme, so no new handler is needed:
# "files" is the unfiltered All view, the rest are CATEGORY_VIEWS keys.
_FILTER_LABELS: tuple[tuple[str, str], ...] = (
    ("files", "All"),
    ("images", "🖼️ Images"),
    ("videos", "🎬 Videos"),
    ("audio", "🎵 Audio"),
    ("docs", "📄 Docs"),
    ("apps", "📦 Apps"),
)


def _filter_keyboard_rows(current: str) -> list[list[InlineKeyboardButton]]:
    """Two rows of filter buttons; the current view is marked with ✓."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, label in _FILTER_LABELS:
        mark = "✓ " if key == current else ""
        row.append(InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"cat:{key}:0"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _list_keyboard(page: int, total: int,
                   prefix: str = "list",
                   current: str = "files") -> InlineKeyboardMarkup:
    """Filter row plus Prev/Next buttons. `prefix` is the callback-data
    prefix: "list" for /list, "cat:<key>" for a category browse (e.g.
    "cat:images"). `current` is the view key for the ✓ mark."""
    pages = max(1, (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE)
    rows = _filter_keyboard_rows(current)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀ Prev", callback_data=f"{prefix}:{page - 1}"))
    if page < pages - 1:
        buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"{prefix}:{page + 1}"))
    if buttons:
        rows.append(buttons)
    return InlineKeyboardMarkup(rows)


@require_user
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _user_id(update)
    records, total = store().list_files(limit=config.PAGE_SIZE, offset=0,
                                        user_id=uid)
    await update.message.reply_text(
        _list_text(records, 0, total), reply_markup=_list_keyboard(0, total)
    )


async def list_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await _callback_allowed(update)
    if uid is None:
        return
    query = update.callback_query
    try:
        page = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        page = 0
    records, total = store().list_files(
        limit=config.PAGE_SIZE, offset=page * config.PAGE_SIZE, user_id=uid
    )
    await query.edit_message_text(
        _list_text(records, page, total), reply_markup=_list_keyboard(page, total)
    )


# ---------------------------------------------------------------------------
# Category browsing: /files, /images, /videos, /audio, /docs, /apps.
# Each is a filtered, paged view of the requesting user's vault, reusing
# the list helpers. Key -> (store category, filename extensions, title, emoji).
# APKs/archives live in their own "apps" category (filed by extension at
# save time), so /docs — which filters category "documents" — never shows
# them. The extension filter on "apps" stays as a safety net.
# ---------------------------------------------------------------------------

CATEGORY_VIEWS: dict[str, tuple[str | None, tuple[str, ...] | None, str, str]] = {
    "files": (None, None, "Your files", "📁"),
    "images": ("photos", None, "Your images", "🖼️"),
    "videos": ("videos", None, "Your videos", "🎬"),
    "audio": ("audio", None, "Your audio", "🎵"),
    "docs": ("documents", None, "Your documents", "📄"),
    "apps": ("apps", APP_ARCHIVE_EXTENSIONS, "Your APKs & archives", "📦"),
}


async def _send_category_list(update: Update, key: str, page: int = 0,
                              edit: bool = False) -> None:
    """Send (or edit to) one page of a category view, for one user."""
    category, extensions, title, emoji = CATEGORY_VIEWS[key]
    uid = _callback_user_id(update) if edit else _user_id(update)
    records, total = store().list_files(
        limit=config.PAGE_SIZE, offset=page * config.PAGE_SIZE,
        category=category, extensions=extensions, user_id=uid,
    )
    text = _list_text(records, page, total, title=title, emoji=emoji)
    markup = _list_keyboard(page, total, prefix=f"cat:{key}", current=key)
    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


def _category_command(key: str):
    """Build a command handler for one category view (any user)."""
    @require_user
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _send_category_list(update, key)
    return handler


files_cmd = _category_command("files")
images_cmd = _category_command("images")
videos_cmd = _category_command("videos")
audio_cmd = _category_command("audio")
docs_cmd = _category_command("docs")
apps_cmd = _category_command("apps")


async def category_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prev/Next paging for a category view (callback data "cat:<key>:<page>")."""
    uid = await _callback_allowed(update)
    if uid is None:
        return
    query = update.callback_query
    try:
        _, key, page_s = query.data.split(":", 2)
        page = int(page_s)
    except (ValueError, IndexError):
        return
    if key not in CATEGORY_VIEWS:
        return
    await _send_category_list(update, key, page=page, edit=True)


# ---------------------------------------------------------------------------
# /clear — wipe the REQUESTING user's vault only, and ONLY after an
# inline confirmation tap. The command itself never deletes anything.
# ---------------------------------------------------------------------------

@require_user
async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _user_id(update)
    n_files = store().stats(user_id=uid)["total_files"]
    _, n_notes = store().list_notes(limit=1, user_id=uid)
    _, n_links = store().list_links(limit=1, user_id=uid)
    if n_files == 0 and n_notes == 0 and n_links == 0:
        await update.message.reply_text(
            "Your vault is already empty — nothing to wipe."
        )
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, wipe everything", callback_data="clear:yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="clear:no")],
    ])
    await update.message.reply_text(
        f"⚠️ This will permanently delete {n_files} file(s), {n_notes} "
        f"note(s), and {n_links} link(s) from YOUR vault. This cannot be "
        "undone.\n\nAre you sure?",
        reply_markup=keyboard,
    )


async def clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /clear confirmation buttons ("clear:yes" / "clear:no")."""
    uid = await _callback_allowed(update)
    if uid is None:
        return
    query = update.callback_query
    action = query.data.split(":", 1)[1] if ":" in query.data else ""
    if action == "yes":
        _, n_links = store().list_links(limit=1, user_id=uid)
        n_files, n_notes = store().clear_all(user_id=uid)
        await query.edit_message_text(
            f"🗑️ Vault wiped: {n_files} file(s), {n_notes} note(s), and "
            f"{n_links} link(s) deleted. The Sanctum is empty."
        )
    else:
        await query.edit_message_text(
            "Wipe cancelled — nothing was deleted. Your vault is safe."
        )


@require_user
async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /search <text>")
        return
    results = store().search_files(query, user_id=_user_id(update))
    if not results:
        await update.message.reply_text(f"Nothing matches '{query}'.")
        return
    lines = [f"🔍 {len(results)} match(es) for '{query}':"]
    lines += [f"• {r.display()}" for r in results]
    lines.append("\n/get <id or name> to download one.")
    await update.message.reply_text("\n".join(lines))


def _resolve_or_reply(update: Update, arg: str, user_id: int):
    """Resolve an id/name to one record in the user's own vault,
    replying on failure/ambiguity. Another user's file is simply
    'not found' — never revealed."""
    matches = store().resolve(arg, user_id=user_id)
    if not matches:
        return None, f"No file matches '{arg}'. Use /list to see what you have."
    if len(matches) > 1:
        lines = [f"Several files match '{arg}' — be specific:"]
        lines += [f"• {r.display()}" for r in matches[:10]]
        return None, "\n".join(lines)
    return matches[0], None


async def _send_by_file_id(update: Update, rec: FileRecord, caption: str) -> None:
    """Send a vault file back using its Telegram file_id (no download)."""
    message = update.message
    fid = rec.telegram_file_id
    if rec.file_type == "photo":
        await message.reply_photo(photo=fid, caption=caption)
    elif rec.file_type == "video":
        await message.reply_video(video=fid, caption=caption)
    elif rec.file_type == "voice":
        await message.reply_voice(voice=fid, caption=caption)
    elif rec.file_type == "audio":
        await message.reply_audio(audio=fid, caption=caption)
    else:
        await message.reply_document(
            document=fid, filename=rec.filename, caption=caption
        )


def _large_file_details(rec: FileRecord) -> str:
    """What to tell the user when a large file cannot be sent back:
    the file's details, so they can find it in chat history by date."""
    return (
        f"📦 #{rec.user_num} {rec.filename}\n"
        f"Size: {human_size(rec.size)} — sent on {rec.date_str}.\n\n"
        "This file is too large for a bot to send back (over 50 MB), and "
        "the original message could not be forwarded — it may have been "
        "deleted. If you cleared this chat, that original message is gone, "
        "and I can't retrieve this file anymore."
    )


@require_user
async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /get <number or name>")
        return
    uid = _user_id(update)
    rec, err = _resolve_or_reply(update, " ".join(context.args), uid)
    if err:
        await update.message.reply_text(err)
        return
    caption = f"#{rec.user_num} {rec.filename}"
    if rec.telegram_file_id:
        # Stored by reference on Telegram — never downloaded.
        if rec.size <= config.MAX_SEND_BYTES:
            try:
                await _send_by_file_id(update, rec, caption)
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                log.exception("Failed to send file #%s by file_id", rec.id)
                await update.message.reply_text(
                    f"⚠️ Telegram wouldn't send #{rec.user_num} {rec.filename} "
                    f"back: {exc}"
                )
            return
        # Over 50 MB: a bot cannot send it, but it CAN forward the
        # original message that carried the file — forwarding re-delivers
        # the file already on Telegram's servers, without re-uploading.
        if rec.source_chat_id and rec.source_message_id:
            try:
                await context.bot.forward_message(
                    chat_id=update.effective_chat.id,
                    from_chat_id=rec.source_chat_id,
                    message_id=rec.source_message_id,
                )
                return
            except Exception:  # noqa: BLE001 - fall through to details
                log.exception(
                    "Forward of large file #%s failed", rec.id
                )
        await update.message.reply_text(_large_file_details(rec))
        return
    # Old on-disk record (from before the file_id switch): serve from disk.
    path: Path = rec.path
    if not path.exists():
        await update.message.reply_text(
            f"⚠️ The index knows #{rec.user_num} {rec.filename}, but the file is "
            "missing from disk. It may have been removed outside the bot."
        )
        return
    if rec.category == "photos":
        with open(path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=caption)
    elif rec.category == "videos":
        with open(path, "rb") as f:
            await update.message.reply_video(video=f, caption=caption)
    elif rec.category == "audio":
        with open(path, "rb") as f:
            await update.message.reply_audio(audio=f, caption=caption)
    else:
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename=rec.filename, caption=caption)


@require_user
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /delete <number or name>")
        return
    uid = _user_id(update)
    rec, err = _resolve_or_reply(update, " ".join(context.args), uid)
    if err:
        await update.message.reply_text(err)
        return
    store().delete_file(rec.user_num, user_id=uid)
    await update.message.reply_text(f"🗑️ Deleted #{rec.user_num} {rec.filename}.")


@require_user
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store().stats(user_id=_user_id(update))
    _, n_notes = store().list_notes(limit=1, user_id=_user_id(update))
    _, n_links = store().list_links(limit=1, user_id=_user_id(update))
    lines = [
        "📊 *Your vault stats*",
        f"Files: {s['total_files']} (limit {MAX_FILES_PER_USER})",
        f"Total size: {human_size(s['total_size'])} "
        f"(limit {human_size(MAX_BYTES_PER_USER)})",
        f"Notes: {n_notes} (limit {MAX_NOTES_PER_USER})",
        f"Links: {n_links} (limit {MAX_LINKS_PER_USER})",
    ]
    # Friendly names for the stored categories, shared with /ask via
    # vault.CATEGORY_LABELS so both commands say the same thing.
    for cat, info in sorted(s["by_category"].items()):
        label = CATEGORY_LABELS.get(cat, cat)
        lines.append(f"• {label}: {info['count']} file(s), {human_size(info['size'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Saving incoming files (scoped to the sender's vault, quota-enforced)
# ---------------------------------------------------------------------------

def _incoming_file(message):
    """Return (file_id, file_type, filename, size) for an incoming message."""
    if message.document:
        d = message.document
        return d.file_id, "document", d.file_name or "file", d.file_size or 0
    if message.photo:
        p = message.photo[-1]  # largest size
        stamp = datetime.fromtimestamp(message.date.timestamp()).strftime("%Y%m%d_%H%M%S")
        return p.file_id, "photo", f"photo_{stamp}.jpg", p.file_size or 0
    if message.video:
        v = message.video
        stamp = datetime.fromtimestamp(message.date.timestamp()).strftime("%Y%m%d_%H%M%S")
        return v.file_id, "video", v.file_name or f"video_{stamp}.mp4", v.file_size or 0
    if message.audio:
        a = message.audio
        stamp = datetime.fromtimestamp(message.date.timestamp()).strftime("%Y%m%d_%H%M%S")
        return a.file_id, "audio", a.file_name or f"audio_{stamp}.mp3", a.file_size or 0
    if message.voice:
        v = message.voice
        stamp = datetime.fromtimestamp(message.date.timestamp()).strftime("%Y%m%d_%H%M%S")
        return v.file_id, "voice", f"voice_{stamp}.ogg", v.file_size or 0
    return None


@require_user
async def save_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store an incoming file BY REFERENCE in the sender's own vault:
    keep its Telegram file_id, name, size, and the source message —
    never download it. That removes the old 20 MB download limit
    entirely; files of any size are accepted. Files up to 50 MB are
    sent back by file_id; larger ones are sent back by forwarding this
    original message (see get_cmd). Per-user limits are enforced first.
    """
    message = update.message
    incoming = _incoming_file(message)
    if incoming is None:
        await message.reply_text("I can't store that kind of message.")
        return
    file_id, file_type, filename, size = incoming
    uid = _user_id(update)
    # Duplicate check first: if this exact file (same Telegram file_id)
    # is already in the sender's vault, do NOT store a second row —
    # point at the original instead. A duplicate costs nothing: no
    # quota, no daily-limit slot. The match is per-user, so the same
    # file in another user's vault is not a duplicate.
    existing = store().find_by_telegram_file_id(file_id, user_id=uid)
    if existing is not None:
        await message.reply_text(
            f"✅ Already in your vault as #{existing.user_num} "
            f"{existing.filename} "
            f"({existing.category}, {human_size(existing.size)}).\n"
            f"Use /get {existing.user_num} to get it back."
        )
        return
    limit_error = store().quota_check(uid, size)
    if limit_error:
        await message.reply_text(f"⚠️ {limit_error}")
        return
    try:
        rec = store().add_file(
            user_id=uid,
            file_id=file_id,
            filename=filename,
            file_type=file_type,
            size=size,
            caption=message.caption or "",
            telegram_file_id=file_id,
            source_chat_id=message.chat_id,
            source_message_id=message.message_id,
        )
    except Exception as exc:  # noqa: BLE001 - report, don't crash the bot
        log.exception("Failed to store a file")
        await message.reply_text(f"⚠️ Couldn't store that file: {exc}")
        return
    reply = (
        f"✅ Stored #{rec.user_num} {rec.filename} "
        f"({rec.category}, {human_size(rec.size)}).\n"
        f"Use /get {rec.user_num} to get it back."
    )
    if size and size > config.MAX_SEND_BYTES:
        reply += (
            f"\n\nNote: this is a large file ({human_size(size)}). Telegram "
            "bots can't send files over 50 MB, so when you ask for it back "
            "I'll forward the original message instead. If that message is "
            "gone — for example, you cleared this chat — I won't be able "
            "to get it back at all."
        )
    await message.reply_text(reply)


# ---------------------------------------------------------------------------
# Notes (scoped to the requesting user's notes)
# ---------------------------------------------------------------------------

@require_user
async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /note <text> to save, or /note <id> to read.")
        return
    uid = _user_id(update)
    arg = " ".join(context.args)
    if arg.isdigit():
        nid = _parse_id_arg(arg)
        note = store().get_note(nid, user_id=uid) if nid is not None else None
        if note is None:
            await update.message.reply_text(f"No note #{arg}.")
        else:
            await update.message.reply_text(f"📝 Note #{note.id}:\n{note.text}")
        return
    try:
        note = store().add_note(arg, user_id=uid)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text(f"📝 Saved note #{note.id}.")


@require_user
async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    notes, total = store().list_notes(limit=config.PAGE_SIZE,
                                      user_id=_user_id(update))
    if total == 0:
        await update.message.reply_text("You have no notes yet. Save one with /note <text>.")
        return
    lines = [f"📝 Your notes ({total} total):"]
    for n in notes:
        preview = n.text if len(n.text) <= 120 else n.text[:117] + "…"
        lines.append(f"• #{n.id}: {preview}")
    await update.message.reply_text("\n".join(lines))


@require_user
async def delnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    nid = _parse_id_arg(context.args[0]) if context.args else None
    if nid is None:
        await update.message.reply_text("Usage: /delnote <id>")
        return
    note = store().delete_note(nid, user_id=_user_id(update))
    if note is None:
        await update.message.reply_text(f"No note #{context.args[0]}.")
    else:
        await update.message.reply_text(f"🗑️ Deleted note #{note.id}.")


# ---------------------------------------------------------------------------
# Links (scoped to the requesting user's links) + the AI-key rule used
# by /analyse.
# ---------------------------------------------------------------------------

# A bare URL sent on its own is saved as a link (see plain_text below).
# Same shape as Store.normalise_url, but usable on raw message text.
_BARE_URL_RE = re.compile(
    r"^(?:https?://)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"(?::\d{1,5})?(?:/[^\s]*)?$"
)


def _resolve_ai_key(uid: int | None) -> tuple[str | None, bool]:
    """Which Gemini key may answer this user — the SAME rule as /chat,
    no exceptions, no fallback in either direction:

    * the owner (OWNER_ID) -> the owner's key from .env (GEMINI_API_KEY)
    * any other user        -> their OWN linked key (/setkey), never the
      owner's key
    * no key at all         -> (None, False): the caller refuses with
      BYOK instructions.

    Returns (key, user_key): `user_key` is True when the key is a user's
    own linked key, so failure advice can point at /setkey; False when
    it is the owner's key from .env. The owner's key is NEVER returned
    for a non-owner, and a non-owner's linked key is NEVER used for the
    owner."""
    if uid is None:
        return None, False
    if is_owner(uid):
        return config.GEMINI_API_KEY, False
    return store().get_user_key(uid), True


def _links_list_text(links: list[LinkRecord], page: int, total: int) -> str:
    if total == 0:
        return (
            "You have no saved links yet. Save one with /links <url> "
            "[title], or just send a link on its own."
        )
    pages = max(1, (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE)
    lines = [f"🔗 Your links — page {page + 1} of {pages} ({total} total):"]
    lines += [f"• {link.display()}" for link in links]
    lines.append("\n/links <id> to show one link in full.")
    return "\n".join(lines)


def _links_keyboard(page: int, total: int) -> InlineKeyboardMarkup | None:
    pages = max(1, (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀ Prev", callback_data=f"links:{page - 1}"))
    if page < pages - 1:
        buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"links:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


@require_user
async def links_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /links — four uses, all scoped to the requesting user's links:
    * /links                    -> paged list of saved links
    * /links <url> [title]      -> save a link
    * /links <id>               -> show one saved link in full
    * /links <text>             -> search saved links by URL or title
    """
    uid = _user_id(update)
    args = context.args
    if not args:
        links, total = store().list_links(limit=config.PAGE_SIZE, offset=0,
                                          user_id=uid)
        await update.message.reply_text(
            _links_list_text(links, 0, total),
            reply_markup=_links_keyboard(0, total),
        )
        return
    first = args[0]
    if first.isdigit():
        lid = _parse_id_arg(first)
        link = store().get_link(lid, user_id=uid) if lid is not None else None
        if link is None:
            await update.message.reply_text(f"No saved link #{first}.")
        else:
            title = f"{link.title}\n" if link.title else ""
            await update.message.reply_text(f"🔗 Link #{link.id}:\n{title}{link.url}")
        return
    # A URL (with or without a scheme) is a save; anything else searches.
    url = Store.normalise_url(first)
    if url is not None:
        title = " ".join(args[1:])
        try:
            link = store().add_link(url, title, user_id=uid)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(f"🔗 Saved link #{link.id}: {link.url}")
        return
    query = " ".join(args)
    results = store().search_links(query, user_id=uid)
    if not results:
        await update.message.reply_text(f"No saved links match '{query}'.")
        return
    lines = [f"🔍 {len(results)} saved link(s) for '{query}':"]
    lines += [f"• {link.display()}" for link in results]
    lines.append("\n/links <id> to show one link in full.")
    await update.message.reply_text("\n".join(lines))


async def links_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prev/Next paging for /links (callback data "links:<page>")."""
    uid = await _callback_allowed(update)
    if uid is None:
        return
    query = update.callback_query
    try:
        page = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        page = 0
    links, total = store().list_links(
        limit=config.PAGE_SIZE, offset=page * config.PAGE_SIZE, user_id=uid
    )
    await query.edit_message_text(
        _links_list_text(links, page, total),
        reply_markup=_links_keyboard(page, total),
    )


@require_user
async def dellink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lid = _parse_id_arg(context.args[0]) if context.args else None
    if lid is None:
        await update.message.reply_text("Usage: /dellink <id>")
        return
    link = store().delete_link(lid, user_id=_user_id(update))
    if link is None:
        await update.message.reply_text(f"No saved link #{context.args[0]}.")
    else:
        await update.message.reply_text(f"🗑️ Deleted link #{link.id}.")


# ---------------------------------------------------------------------------
# /analyse — AI summary of vault text files and PDFs.
#
# It uses Gemini text through ai.py, under the SAME BYOK rule as /chat
# (see _resolve_ai_key): the owner's .env key answers the owner only;
# any other user needs their own linked key; no key means a polite BYOK
# refusal and NO API call. Nothing is ever sent to Gemini without a key
# that belongs to the user asking. Images are never sent: /analyse
# refuses them before any download or AI call (Nov 15, 2026, owner's
# decision — /ocr was removed the same day for the same reason).
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
# Plain-text-ish files /analyse can read directly (by extension).
_TEXT_EXTENSIONS = (
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".html",
    ".htm", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".sh",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".tex", ".rst",
)
def _is_image_record(rec: FileRecord) -> bool:
    """True when a vault record is an image (photo, or a document whose
    filename ends in an image extension)."""
    if rec.file_type == "photo":
        return True
    return rec.filename.lower().endswith(_IMAGE_EXTENSIONS)


# Waits (seconds) before the 2nd and 3rd download attempts in
# _download_via_bot. Module-level so tests can patch it to (0, 0).
_DOWNLOAD_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.5)


async def _download_via_bot(context: ContextTypes.DEFAULT_TYPE,
                            file_id: str,
                            attempts: int = 3) -> bytes | None:
    """Download a Telegram file by file_id (Bot API getFile, 20 MB
    limit). Retries transient failures: the egress proxy drops
    connections now and then, and a single failed download used to
    surface as "couldn't download" even when the file itself is fine
    (verified live: a 363 B txt failed once, then downloaded cleanly
    moments later). Returns the bytes, or None after all attempts
    fail. Never logs the file contents."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            tg_file = await context.bot.get_file(file_id)
            data = await tg_file.download_as_bytearray()
            return bytes(data)
        except Exception as exc:  # noqa: BLE001 - retry, then soft-fail
            last_exc = exc
            if attempt + 1 < attempts:
                delay = (
                    _DOWNLOAD_RETRY_DELAYS[attempt]
                    if attempt < len(_DOWNLOAD_RETRY_DELAYS)
                    else _DOWNLOAD_RETRY_DELAYS[-1]
                )
                await asyncio.sleep(delay)
    log.exception(
        "Failed to download a file for /analyse after %d attempts",
        attempts,
    )
    return None


async def _record_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        rec: FileRecord) -> bytes | None:
    """Bytes of a vault record, from Telegram (file_id) or from disk
    (old on-disk records). Returns None when the bytes cannot be got,
    or when the file is over the Bot API download limit."""
    if rec.telegram_file_id:
        if rec.size and rec.size > config.MAX_ANALYSE_DOWNLOAD_BYTES:
            return None
        return await _download_via_bot(context, rec.telegram_file_id)
    path: Path = rec.path
    if not path.exists():
        return None
    if rec.size and rec.size > config.MAX_ANALYSE_DOWNLOAD_BYTES:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _pdf_to_text(data: bytes) -> str | None:
    """Extract text from PDF bytes with `pdftotext` (poppler), if it is
    installed on this machine. Returns the text, or None when pdftotext
    is missing or fails. The bytes are passed on stdin — nothing is
    written to disk. No new dependency: pdftotext ships with poppler."""
    if shutil.which("pdftotext") is None:
        return None
    try:
        proc = subprocess.run(
            ["pdftotext", "-", "-"], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _extract_text(data: bytes, filename: str) -> str | None:
    """Extract readable text from downloaded file bytes, or None when
    the file has no extractable text here. PDF goes through pdftotext;
    anything else is decoded as UTF-8 (replacing bad bytes) and refused
    when it looks binary (NUL bytes). Returns None for binary content —
    the caller reports metadata instead of guessing."""
    if filename.lower().endswith(".pdf"):
        text = _pdf_to_text(data)
        return text.strip() if text and text.strip() else None
    if b"\x00" in data[:4096]:
        return None
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - undecodable bytes are not text
        return None
    return text.strip() if text.strip() else None


def _unsupported_file_text(rec: FileRecord, reason: str) -> str:
    """What /analyse says for a file it cannot read: the file's facts
    and a plain explanation — never a hallucinated summary."""
    return (
        f"🔍 #{rec.user_num} {rec.filename}\n"
        f"Type: {rec.category} ({rec.file_type}), "
        f"size {human_size(rec.size)}, sent {rec.date_str}.\n\n"
        f"{reason}"
    )


@require_user
async def analyse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """AI analysis of one vault file: /analyse <file number or name>.

    * Text files and PDFs: the text is extracted (pdftotext for PDFs)
      and Gemini summarises it — key points, what it is about.
    * Images: refused — the keeper no longer reads images (Nov 15,
      2026, owner's decision). No download, no AI call.
    * Audio, video, archives, and anything else unreadable: the bot
      replies with the file's facts and a plain explanation of why it
      cannot analyse it — it never invents a summary.
    Files over the 20 MB Bot API download limit are refused politely.
    BYOK applies exactly as for /chat (see _resolve_ai_key)."""
    if not context.args:
        await update.message.reply_text("Usage: /analyse <file number or name>")
        return
    uid = _user_id(update)
    rec, err = _resolve_or_reply(update, " ".join(context.args), uid)
    if err:
        await update.message.reply_text(err)
        return
    if rec.size and rec.size > config.MAX_ANALYSE_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"#{rec.user_num} {rec.filename} is {human_size(rec.size)} — "
            "too large to download for analysis (the limit is "
            f"{human_size(config.MAX_ANALYSE_DOWNLOAD_BYTES)}). You can "
            "still get it back with /get."
        )
        return
    # Images are refused outright — the keeper no longer reads images
    # (Nov 15, 2026, owner's decision: vision calls cost too much
    # bandwidth on the free-tier host). No download, no AI call, no key
    # needed for the refusal.
    if _is_image_record(rec):
        await update.message.reply_text(
            f"🔮 I no longer read images, traveller — #{rec.user_num} "
            f"{rec.filename} stays unread. /analyse works on text files "
            "and PDFs only. Your image is safe in the vault; use /get "
            "to fetch it back."
        )
        return
    # Unsupported kinds need no AI at all: answer from metadata alone,
    # so no key is needed for them either.
    name = rec.filename.lower()
    if name.endswith(".pdf") or name.endswith(_TEXT_EXTENSIONS):
        kind = "text"
    else:
        await update.message.reply_text(_unsupported_file_text(
            rec,
            "I can't analyse this kind of file — there is no text in it "
            "for me to read, and this bot has no audio/video transcription. "
            "Text files and PDFs are the ones /analyse can read.",
        ))
        return
    key, user_key = _resolve_ai_key(uid)
    if key is None:
        await update.message.reply_text(BYOK_NO_KEY_TEXT)
        return
    data = await _record_bytes(update, context, rec)
    if data is None:
        await update.message.reply_text(
            f"⚠️ I couldn't download #{rec.user_num} {rec.filename} from "
            "Telegram to analyse it. Try again, or use /get to fetch it."
        )
        return
    text = _extract_text(data, rec.filename)
    if text is None:
        await update.message.reply_text(_unsupported_file_text(
            rec,
            "I couldn't pull any readable text out of this file — it may "
            "be scanned images inside a PDF, or binary data. If it is a "
            "scanned PDF, its pages are images, which I cannot read.",
        ))
        return
    clipped = text if len(text) <= MAX_ANALYSE_TEXT_CHARS else (
        text[:MAX_ANALYSE_TEXT_CHARS]
        + f"\n\n[… {len(text) - MAX_ANALYSE_TEXT_CHARS} more characters not shown to the AI.]"
    )
    reply = await analyze_text_with_ai(
        uid,
        "Summarise this file concisely: what it is, the key points, and "
        f"anything important. File: {rec.filename}\n\n{clipped}",
        api_key=key, user_key=user_key,
    )
    await update.message.reply_text(
        f"🔍 Analysis of #{rec.user_num} {rec.filename}:\n\n{reply}"[:4000]
    )

@require_user
async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    minutes = _parse_id_arg(context.args[0]) if context.args else None
    if minutes is None or len(context.args) < 2:
        await update.message.reply_text("Usage: /remind <minutes> <text>")
        return
    if minutes < 1 or minutes > 60 * 24 * 7:
        await update.message.reply_text("Minutes must be between 1 and 10080 (one week).")
        return
    text = " ".join(context.args[1:])
    job_queue = context.application.job_queue
    if job_queue is None:
        await update.message.reply_text("Reminders are not available (JobQueue missing).")
        return
    # Per-user cap on ACTIVE reminders (anti-abuse): count this chat's
    # pending reminder jobs before scheduling another one.
    chat_id = update.effective_chat.id
    try:
        active = [
            job for job in job_queue.jobs()
            if (job.name or "").startswith(f"remind-{chat_id}-")
        ]
    except Exception:  # noqa: BLE001 - a broken JobQueue must not wedge /remind
        active = []
    if len(active) >= MAX_REMINDERS_PER_USER:
        await update.message.reply_text(
            f"⚠️ You already have {MAX_REMINDERS_PER_USER} active reminders — "
            "the limit per user. Wait for one to fire, or ask the keeper."
        )
        return
    schedule_reminder(job_queue, chat_id, minutes, text)
    await update.message.reply_text(f"⏰ I will remind you in {minutes} minute(s): {text}")


@require_user
async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args)
    if not question:
        await update.message.reply_text("Ask me something: /ask <question>")
        return
    await update.message.reply_text(
        answer_ask(question, store(), _user_id(update))
    )


# ---------------------------------------------------------------------------
# /chat — the ONLY path that calls the external AI (Gemini), and only ever
# with the key that belongs to the user asking:
#   * the owner       -> the owner's key from .env (GEMINI_API_KEY)
#   * any other user  -> their OWN linked key (/setkey), never the owner's
#   * no key at all   -> a reply explaining how to link their own free key
# The vault works fully without AI, and plain chat stays local.
# ---------------------------------------------------------------------------

NO_KEY_ASIDE = (
    "\n\n— One more thing, keeper: a fuller mind could dwell in these halls, "
    "if you ever gift the Sanctum a free Gemini key. Until then, my own local "
    "wit will have to do."
)

BYOK_NO_KEY_TEXT = (
    "To wake the greater mind, the Sanctum needs a key of your own — I "
    "never spend the keeper's key on other travellers. Get a free one:\n\n"
    "1. Open https://aistudio.google.com/apikey\n"
    "2. Sign in with Google and tap “Create API key”\n"
    "3. Copy the key and send it here: /setkey <your key>\n\n"
    "Until then, the vault works fully without AI, and plain chat still "
    "answers in character."
)


@require_user
async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Talk with the Sanctum AI — the keeper ONLY (Nov 15, 2026,
    owner's decision). Any other user gets a short refusal: no AI call,
    no key check, no BYOK prompt. /analyse stays BYOK for everyone."""
    uid = _user_id(update)
    if not is_owner(uid):
        await update.message.reply_text(
            "🔮 The keeper's counsel is for the keeper alone, "
            "traveller — /chat answers only its keeper. Your vault, "
            "notes, and /analyse are still yours to use."
        )
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Speak, keeper: /chat <message> — for deeper conversation. "
            "For quick things, just talk to me, or use /ask about the vault."
        )
        return
    # The owner: the owner's key from .env, passed EXPLICITLY —
    # chat_with_ai never falls back to it on its own.
    if config.GEMINI_API_KEY:
        reply = await chat_with_ai(
            uid, text, api_key=config.GEMINI_API_KEY,
        )
    else:
        reply = answer_chat(text, store(), uid, is_owner=True) + NO_KEY_ASIDE
    await update.message.reply_text(reply)

@require_user
async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-text messages (not commands, not files).

    A BARE LINK on its own (just a URL, nothing else) is SAVED as a
    link in the sender's own vault — that is the quickest way to keep a
    link, and a lone URL is almost never chitchat. Anything else is
    answered in character with the LOCAL assistant. Never sent to the
    AI, never silent."""
    text = (update.message.text or "").strip()
    if not text:
        return
    uid = _user_id(update)
    if _BARE_URL_RE.match(text):
        url = Store.normalise_url(text)
        if url is not None:
            try:
                link = store().add_link(url, "", user_id=uid)
            except ValueError as exc:
                await update.message.reply_text(str(exc))
                return
            await update.message.reply_text(
                f"🔗 Saved link #{link.id}: {link.url}\n"
                "See all your links with /links, or remove it with "
                f"/dellink {link.id}."
            )
            return
    await update.message.reply_text(
        answer_chat(text, store(), uid, is_owner=is_owner(uid))
    )


@require_user
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "I don't know that command. Use /help to see everything I can do."
    )


# ---------------------------------------------------------------------------
# Bring-your-own-key: /setkey, /delkey, /mykey
# Keys are stored encrypted at rest (see keystore.py) and are NEVER shown
# back, echoed, or logged — not even partially.
# ---------------------------------------------------------------------------

MIN_KEY_LENGTH = 20


def _looks_like_a_key(key: str) -> bool:
    """A Gemini API key is a long token with no whitespace. This is only
    a shape check — the real test is the first /chat call with it."""
    return len(key) >= MIN_KEY_LENGTH and not any(ch.isspace() for ch in key)


@require_user
async def setkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = " ".join(context.args).strip()
    if not _looks_like_a_key(key):
        await update.message.reply_text(
            "That doesn't look like a Gemini API key — it should be a long "
            "string with no spaces. Copy it from "
            "https://aistudio.google.com/apikey and send "
            "/setkey <key> again."
        )
        return
    try:
        store().set_user_key(_user_id(update), key)
    except RuntimeError:
        # The key vault has no encryption secret (see keystore.py) — a
        # clean failure, never a crash, and the key is never logged.
        log.exception("Failed to store a linked key (key vault unavailable)")
        await update.message.reply_text(
            "⚠️ I couldn't store your key right now — the Sanctum's key "
            "vault is unavailable. Try again later, or contact the keeper."
        )
        return
    await update.message.reply_text(
        "🔑 Your key is linked. /chat will now use your own key and your "
        "own free allowance — never the keeper's. I never show your key "
        "back to anyone; check with /mykey. To remove it, use /delkey."
    )


@require_user
async def delkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    had_key = store().delete_user_key(_user_id(update))
    if had_key:
        await update.message.reply_text(
            "🔑 Your linked key has been removed. /chat will ask you to "
            "link a new one if you want the AI back."
        )
    else:
        await update.message.reply_text(
            "You had no key linked. Link one with /setkey <key> if you "
            "want the AI in /chat."
        )


@require_user
async def mykey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    has_key = store().get_user_key(_user_id(update)) is not None
    if has_key:
        await update.message.reply_text(
            "🔑 Your Gemini key: linked. /chat uses your own key."
        )
    else:
        await update.message.reply_text(
            "🔑 Your Gemini key: not linked. /chat needs your own free "
            "key — link one with /setkey <key> "
            "(get one at https://aistudio.google.com/apikey)."
        )


# ---------------------------------------------------------------------------
# Admin (owner only): aggregate numbers, ban/unban.
# /admin shows totals only — no file names, no contents, no per-user data.
# ---------------------------------------------------------------------------

# India is UTC+5:30 all year (no daylight saving), so a fixed offset
# is exact — no tz database needed.
IST = timezone(timedelta(hours=5, minutes=30))

# /users shows at most this many people; the rest collapse into a
# single "…and N more users" line.
_MAX_USERS_LISTED = 25


def _ist_date(epoch: int) -> str:
    """Unix epoch → "15 Nov 2026" in IST."""
    return datetime.fromtimestamp(epoch, tz=IST).strftime("%d %b %Y")


def _ist_datetime(epoch: int) -> str:
    """Unix epoch → "15 Nov 2026, 20:14" in IST."""
    return datetime.fromtimestamp(epoch, tz=IST).strftime("%d %b %Y, %H:%M")


def _count_str(n: int, singular: str) -> str:
    """"1 note" / "3 notes" — plain plural, all three nouns are regular."""
    return f"{n} {singular if n == 1 else singular + 's'}"


async def _resolve_user_name(context: ContextTypes.DEFAULT_TYPE,
                             user_id: int) -> str | None:
    """Live display name for a user id: first name (+ last name) and
    @username when Telegram has one. Resolved at display time only —
    names are never stored. Returns None when Telegram cannot tell us
    (user blocked the bot, deleted account, any error), and the caller
    then shows the bare id instead."""
    try:
        chat = await context.bot.get_chat(user_id)
    except Exception:  # noqa: BLE001 - any lookup failure → id-only line
        return None
    name = " ".join(
        part for part in (chat.first_name, chat.last_name) if part
    )
    username = getattr(chat, "username", None)
    if username:
        name = f"{name} (@{username})" if name else f"@{username}"
    return name or None


@owner_only
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store().aggregate_stats()
    lines = [
        "🛡️ *Sanctum admin*",
        f"Users: {s['total_users']}",
        f"Files: {s['total_files']} "
        f"({human_size(s['total_size'])} reported)",
        f"Notes: {s['total_notes']}",
        f"Links: {s['total_links']}",
        f"Banned users: {s['banned_users']}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Who uses the bot — for the keeper's eyes only. One line per
    user: live name (never stored), Telegram id, first-seen date,
    last-seen date+time ("—" when they joined before last-seen
    tracking and have not interacted since), and their file/note/link
    counts. A failed name lookup never breaks the list: that user is
    shown by id alone."""
    users = store().list_users()
    lines = [f"👥 Sanctum users — {len(users)} total"]
    for user in users[:_MAX_USERS_LISTED]:
        name = await _resolve_user_name(context, user["user_id"])
        who = (
            f"{name} — ID {user['user_id']}" if name
            else f"ID {user['user_id']}"
        )
        last_seen = (
            _ist_datetime(user["last_seen"])
            if user["last_seen"] else "—"
        )
        line = (
            f"• {who} · since "
            f"{_ist_date(user['created_at'])} · last seen {last_seen} · "
            f"{_count_str(user['files'], 'file')}, "
            f"{_count_str(user['notes'], 'note')}, "
            f"{_count_str(user['links'], 'link')}"
        )
        if user["is_banned"]:
            line += " 🚫 banned"
        lines.append(line)
    if len(users) > _MAX_USERS_LISTED:
        lines.append(f"…and {len(users) - _MAX_USERS_LISTED} more users")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _parse_id_arg(context.args[0]) if context.args else None
    if target is None:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    if target == config.OWNER_ID:
        await update.message.reply_text("You cannot ban the keeper.")
        return
    store().set_banned(target, True)
    await update.message.reply_text(
        f"🚫 User {target} is banned from the Sanctum."
    )


@owner_only
async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _parse_id_arg(context.args[0]) if context.args else None
    if target is None:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    store().set_banned(target, False)
    await update.message.reply_text(
        f"✅ User {target} is unbanned — the Sanctum is open to them again."
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # NEVER log the Update itself: its repr includes message text, so a
    # /setkey key (or any other user input) could land in the journal.
    # Log only the ids and the error type/message (truncated).
    update_id = update.update_id if update is not None else None
    uid = update.effective_user.id if update is not None and update.effective_user else None
    chat_id = update.effective_chat.id if update is not None and update.effective_chat else None
    err = context.error
    log.exception(
        "Error in update %s (user=%s, chat=%s): %s: %.200s",
        update_id, uid, chat_id, type(err).__name__, err,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# The Telegram Menu button shows exactly these commands — only commands
# the bot actually implements, in this order. Set on startup via
# set_bot_commands (post_init), so a stale BotFather list can never
# advertise a command the bot does not have.
#
# The Menu is SHORT on purpose: the 11 essentials only. Every other
# implemented command (/list, /images, /videos, /audio, /docs, /apps,
# /delete, /clear, /notes, /delnote, /dellink, /remind, /ask, /setkey,
# /delkey, /mykey) still works when typed — its handler stays registered
# in build_app — and /help lists them all under "More". Only the Menu
# list is trimmed, never the command set.
#
# Two scoped menus (Telegram BotCommand scopes):
#   * BOT_COMMANDS — the DEFAULT menu, shown in every private chat:
#     the 11 essentials, and NOTHING owner-only. Non-owners
#     must never see /admin, /ban, /unban.
#   * ADMIN_COMMANDS — appended to the default list only in the OWNER's
#     own menu (BotCommandScopeChat for chat_id = OWNER_ID).
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "Open the Sanctum"),
    ("help", "Show all commands"),
    ("files", "Browse all stored files"),
    ("search", "Search the vault"),
    ("get", "Download a file back"),
    ("stats", "Your vault statistics"),
    ("note", "Save or read a note"),
    ("links", "Save, list, or show a saved link"),
    ("analyse", "AI summary of a vault file"),
]

# /chat is the keeper's alone (Nov 15, 2026): it lives on the owner's
# menu only, never on the default menu.
KEEPER_COMMANDS: list[tuple[str, str]] = [
    ("chat", "Talk with the Sanctum AI"),
]

ADMIN_COMMANDS: list[tuple[str, str]] = [
    ("admin", "Owner: totals across all users"),
    ("users", "Owner: list users of the bot"),
    ("ban", "Owner: ban a user"),
    ("unban", "Owner: unban a user"),
]


async def set_bot_commands(app) -> None:
    """Publish the two scoped menus (called on startup).

    Default menu (all private chats): BOT_COMMANDS only — no admin
    commands, and no /chat (the keeper's alone). Owner menu (the
    owner's private chat only): BOT_COMMANDS plus KEEPER_COMMANDS
    (/chat) plus ADMIN_COMMANDS. Both are set on every startup, so a
    stale BotFather list can never advertise a command the bot does
    not have, and a non-owner can never see an owner-only command."""
    await app.bot.set_my_commands(
        [BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS],
        scope=BotCommandScopeAllPrivateChats(),
    )
    try:
        await app.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in
             BOT_COMMANDS + KEEPER_COMMANDS + ADMIN_COMMANDS],
            scope=BotCommandScopeChat(chat_id=config.OWNER_ID),
        )
    except BadRequest as exc:
        # The owner has not messaged this bot yet, so Telegram has no
        # chat with the owner to scope a menu to ("Chat not found").
        # The default menu above is already published, so the bot works
        # fine without the owner menu; it will be applied on a later
        # restart once the owner has opened a chat with the bot.
        log.warning("Owner menu not set yet (%s); default menu is live.", exc)


def build_app():
    """Build the Application (separated from main() so tests can import)."""
    config.validate()
    global STORE
    STORE = Store(config.DATA_DIR, config.DB_PATH,
                  owner_id=config.OWNER_ID, key_secret=config.KEY_SECRET)
    app = ApplicationBuilder().token(config.BOT_TOKEN).post_init(set_bot_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(start_choice, pattern=r"^start:(keep|fresh)$"))
    app.add_handler(CallbackQueryHandler(fresh_confirm, pattern=r"^fresh:(yes|no)$"))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CallbackQueryHandler(list_page, pattern=r"^list:\d+$"))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("images", images_cmd))
    app.add_handler(CommandHandler("videos", videos_cmd))
    app.add_handler(CommandHandler("audio", audio_cmd))
    app.add_handler(CommandHandler("docs", docs_cmd))
    app.add_handler(CommandHandler("apps", apps_cmd))
    app.add_handler(CallbackQueryHandler(category_page, pattern=r"^cat:[a-z]+:\d+$"))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(clear_confirm, pattern=r"^clear:(yes|no)$"))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("get", get_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("note", note_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("delnote", delnote_cmd))
    app.add_handler(CommandHandler("links", links_cmd))
    app.add_handler(CallbackQueryHandler(links_page, pattern=r"^links:\d+$"))
    app.add_handler(CommandHandler("dellink", dellink_cmd))
    app.add_handler(CommandHandler("analyse", analyse_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("setkey", setkey_cmd))
    app.add_handler(CommandHandler("delkey", delkey_cmd))
    app.add_handler(CommandHandler("mykey", mykey_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | filters.PHOTO | filters.VIDEO
            | filters.AUDIO | filters.VOICE,
            save_file,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    app = build_app()
    log.info("Sanctum Sanctorum starting — vault at %s", config.DATA_DIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
