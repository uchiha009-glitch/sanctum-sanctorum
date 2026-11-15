# 🏛️ Sanctum Sanctorum

A public Telegram file vault + local assistant bot. Anyone can use it, and
every user gets a private vault of their own — send it any file and it keeps
it safe; ask it about your vault, save notes, set reminders, and chat with
it in character. Everything runs on your own server — no Emergent, no cloud.
AI is optional: `/chat` is the keeper's alone (the owner's own Gemini key),
and `/analyse` is bring-your-own-key — any other user who wants it links
their own free Google Gemini key (`/setkey`). The vault works fully
without AI.

## What it does

**File vault** — send any document, photo, video, audio, or voice note. It is
stored by reference: the bot keeps the file's Telegram ID, name, size, the
date you sent it, and the message it arrived in. Telegram holds the file
itself — nothing is downloaded to this server, so there is no size limit on
what you can store.

| Command | What it does |
|---|---|
| `/files` | Browse all stored files (paged, with Prev/Next buttons and filter buttons: All, Images, Videos, Audio, Docs, Apps) |
| `/images` | Browse images only |
| `/videos` | Browse videos only |
| `/audio` | Browse audio & voice notes only |
| `/docs` | Browse documents only (APKs & archives live under `/apps`) |
| `/apps` | Browse APKs & archives only (.apk, .xapk, .zip, .rar, …) |
| `/list` | List stored files (paged, with Prev/Next buttons) |
| `/search <text>` | Search by filename or caption |
| `/get <number or name>` | Get a file back (sent by file_id, or forwarded if over 50 MB) |
| `/delete <number or name>` | Delete a file (index entry; from disk too, for old on-disk files) |
| `/stats` | File count, total size, breakdown by type |
| `/clear` | Wipe the whole vault (files + notes) — asks for confirmation first |

Files are NOT stored on disk any more. The vault is a small SQLite index
(`data/vault.db`) of Telegram file references — the bot never downloads
anything, so the server's disk never fills up and the old 20 MB download
limit is gone. APKs and archives (.apk, .xapk, .zip, .rar, .7z, .tar, …)
are filed under **apps** by filename extension when they arrive, so
`/apps` finds them and `/docs` never shows them; a database saved before
this change is migrated on open (old APK/archive rows move to `apps`). Files up to 50 MB are sent back by file_id; larger files are
sent back by forwarding the original message that carried them — proven
live with a real 110 MB APK. If the forward fails (the message was
deleted), the bot replies with the file's details — name, size, and the
exact date it was sent — so you can find it in your chat history around
that date. Lists (`/list`, `/files`, `/images`, …) show each file's date
for the same reason. Files stored before this change still live on disk
(`data/documents/`, `data/photos/`, …) and are served from disk exactly as
before.

**Files are numbered per user**: every user's vault numbers its own
files #1, #2, #3… — the numbers you see in `/list` and type to `/get`
and `/delete` are yours alone. (The database still has a global row id
underneath, but it is never shown or accepted.)

**Per-user limits** (anti-abuse): each user may store up to **500 files**,
**5 GB** of reported file sizes in total, **30 new files per day**, **500
notes**, **500 saved links**, and **20 active reminders**. When a limit is hit, the bot says so
politely and stores nothing.

**Admin** (owner only)

| Command | What it does |
|---|---|
| `/admin` | Totals across all users (users, files, notes, banned) — numbers only, no names or contents |
| `/users` | List every user, one line each: live name (resolved from Telegram when you run it — never stored), Telegram ID, first-seen date, last-seen date + time, and their file/note/link counts; banned users are marked 🚫 banned. Capped at 25 users, with an "…and N more users" line beyond that |
| `/ban <user_id>` | Ban a user from the bot (banned users get a polite refusal on every command) |
| `/unban <user_id>` | Unban a user |

**Last seen** (Nov 15, 2026): every interaction with the bot — a command, a plain message, a file sent, a button tapped, an admin command — records a `last_seen` timestamp for that user. It is a timestamp only: no message content is ever stored. Users who joined before tracking began show "—" until they next interact. The `users` table gains the `last_seen` column by migration on first start (existing rows keep 0); a fresh database has it from the start.

**Notes**

| Command | What it does |
|---|---|
| `/note <text>` | Save a text note |
| `/note <id>` | Read a note |
| `/notes` | List your notes |
| `/delnote <id>` | Delete a note |

**Links** (per user, like notes — 500 per user)

| Command | What it does |
|---|---|
| `/links <url> [title]` | Save a link (a bare domain works too — `example.com` becomes `https://example.com`) |
| `/links` | List your saved links (paged, with Prev/Next buttons) |
| `/links <id>` | Show one saved link in full |
| `/links <text>` | Search your saved links by URL or title |
| `/dellink <id>` | Delete a saved link |

Just sending a link on its own (nothing else in the message) saves it too.

**Read & analyse** (AI — bring-your-own-key: the owner's key answers
the owner only; everyone else needs their own linked key, or the
command refuses with instructions to link one)

| Command | What it does |
|---|---|
| `/analyse <number or name>` | AI summary of a vault file: text files and PDFs are read and summarised (key points, what it is about). Images are refused outright — no download, no AI call. Audio, video, archives, and anything unreadable reply with the file's facts and a plain explanation — never an invented summary. Files over 20 MB (the Bot API download limit) are refused politely |

**Local assistant** (rule-based, no external AI)

| Command | What it does |
|---|---|
| `/remind <minutes> <text>` | Set a reminder — the bot messages you when it fires |
| `/ask <question>` | Ask about the vault ("how many files do I have?", "what notes do I have about exams?"), do arithmetic ("what is 45 * 12?"), get the date/time |

Just typing to the bot also works — greetings, "how are you", thanks, and
farewells get an in-character reply from the Sanctum's keeper, and simple
vault questions are answered too. The keeper also has a small book of
jokes of his own: say "tell me a joke" (or "haha" / "stupid bot") and he
answers in character, with no AI involved. Plain chat is local and is
never sent to any AI.

**AI chitchat** (keeper only)

| Command | What it does |
|---|---|
| `/chat <message>` | Talk with the Sanctum AI — real conversation, with memory of the last few exchanges. Keeper only: any other user gets a short refusal, no AI call, no key check. Uses the keeper's key from `.env` |
| `/setkey <key>` | Link your own free Gemini key for `/analyse` (stored encrypted, never shown back) |
| `/delkey` | Remove your linked key |
| `/mykey` | Check whether you have a key linked (only "linked" / "not linked" — the key is never shown) |

`/chat` and `/analyse` are the only things that call the external AI.
`/chat` is the keeper's alone and uses the keeper's key from `.env`
(`GEMINI_API_KEY`). `/analyse` is bring-your-own-key: the owner uses
the `.env` key, any other user uses their OWN linked key, and a user
without a linked key is told how to get a free one instead of getting
an AI reply. The owner's key is never spent on anyone else. Vault
commands, files, notes, `/ask`, and plain chat are never sent to
Gemini — plain chat answers in character from the local assistant,
with no key needed. `/analyse` sends a file's extracted text to
Gemini — never an image. Nothing else is ever sent to Gemini.

**Privacy:** anyone can use the bot, but every user has a private vault.
Every file and note belongs to exactly one Telegram user id, and every
query is filtered by that id — a user can never see, get, search, delete,
or list another user's files or notes, and the owner cannot read other
users' vaults either (the owner is just a user with their own vault, plus
`/admin` totals). Asking for another user's file id is simply "not found".
Files live on Telegram's servers (they always did — you send them through
Telegram); the bot only keeps their IDs. Linked Gemini keys are stored
encrypted at rest (Fernet, keyed by `SANCTUM_KEY_SECRET` or a generated
secret in `data/key_secret`) and are never logged, echoed, or shown back.

## 1. Get a bot token

1. On Telegram, open **@BotFather**.
2. Send `/newbot`, pick a name and a username (must end in `bot`).
3. BotFather replies with a token like `123456789:AAF...`. Keep it secret.
4. Open **@userinfobot** and send any message — it replies with your numeric
   Telegram user id. That is your `OWNER_ID`.

If you already made a Sanctum Sanctorum bot before (e.g. on Emergent), you can
reuse it: in @BotFather send `/mybots`, pick the bot, **API Token** → **Revoke
current token** only if you need a fresh one. Otherwise the old token still
works — just put it in `.env`.

## 1b. Get a free Gemini key (optional — for `/chat` and `/analyse`)

The OWNER's key (for the owner's own `/chat` and `/analyse`) goes in
`.env` as `GEMINI_API_KEY`. Every OTHER user links their own key inside
Telegram with `/setkey <key>` — the owner's key is never spent on them,
and `/chat` stays the keeper's alone.

1. Open **https://aistudio.google.com** and sign in with Google.
2. Click **Get API key** → **Create API key**.
3. Copy the key into your `.env` as `GEMINI_API_KEY=...` (owner only), or
   send it to the bot with `/setkey <key>` (any user).

That is all. The free tier is enough for personal use (it has per-minute
and daily limits). Skip this step entirely and the bot still works: the vault,
notes, reminders, and local chitchat need no key at all, and `/analyse`
simply tells a user how to link their own free key. Nothing else changes.

## 1c. Key encryption secret (optional)

Users' linked Gemini keys are encrypted at rest with Fernet. The encryption
secret comes from `SANCTUM_KEY_SECRET` in `.env` if you set it; otherwise a
random secret is generated once and kept in `data/key_secret` (mode 0600).
Either way the keys stay readable across restarts. If the secret is lost
(e.g. the data directory is wiped without a backup), stored keys become
unreadable and users simply re-link them with `/setkey`.

## 2. Configure

```bash
cp .env.example .env
# edit .env:
TELEGRAM_BOT_TOKEN=123456789:AAF...
OWNER_ID=123456789
SANCTUM_DATA_DIR=./data
GEMINI_API_KEY=   # optional — the OWNER's free key, owner-only, see 1b
SANCTUM_KEY_SECRET=  # optional — encrypts users' linked keys, see 1c
```

## 3. Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

Requires Python 3.11+. The bot uses long polling — no webhook, no public URL.

## 4. Deploy to Koyeb (free tier)

1. Push this folder to a GitHub repo.
2. In Koyeb, create a **Web Service** from that repo (or use the Dockerfile:
   Koyeb detects it automatically).
3. Set environment variables in the Koyeb dashboard:
   `TELEGRAM_BOT_TOKEN`, `OWNER_ID`, `SANCTUM_DATA_DIR=/app/data`, and
   optionally `GEMINI_API_KEY` (free key from https://aistudio.google.com).
4. Attach a **Volume** mounted at `/app/data` so the SQLite index survives
   restarts (without a volume, a redeploy wipes the index — the files
   themselves live on Telegram, so they are not lost, but the bot would
   forget them). The volume only holds the tiny index now, not file bytes.
5. Deploy. The bot starts polling immediately. Check the logs for
   "Sanctum Sanctorum starting".

Notes on the free tier: Koyeb's free instance sleeps after inactivity, and
long polling keeps the process busy, which normally keeps it awake — but a
volume is still required so a restart does not lose your vault.

## 5. Run as a systemd service (VM / server)

See `sanctum-sanctorum.service` (install steps are in the comments at the top
of that file). It restarts the bot automatically if it crashes or the machine
reboots. The watchdog script `ensure-running.sh` (run every 5 minutes by the
`sanctum-watchdog` cron) keeps the bot alive across VM replacements too: it
reinstalls the systemd unit if it is gone, recreates the `sanctum` system
user (uid 999) if it no longer exists, restores traverse permission
(`o+x`) on `/home/hatch` if it was reset, and restarts the service if it
is down.

## 6. Test offline (no token needed)

```bash
.venv/bin/python -m unittest discover -s tests -v
```

This exercises the vault (add / list / search / get / delete / stats), notes,
reminders scheduling, the `/ask` responder, and the public multi-user rules
(isolation between users, the migration, owner-only `/chat` routing,
key storage, per-user limits, `/clear` scoping, ban/unban, `/admin`) — all
with fakes, no Telegram calls.

## Project layout

```
bot.py                     Main bot: handlers, per-user vaults, long polling
vault.py                   SQLite index of Telegram file references + notes
                           (older on-disk files still served from disk);
                           per-user isolation, users table, quotas, bans
keystore.py                Fernet encryption for users' linked Gemini keys
assistant.py               Local assistant: /ask rules, reminders, Sanctum persona
ai.py                      Gemini AI calls (only with an explicitly
                           passed key; /chat is the keeper's alone,
                           /analyse is BYOK)
config.py                  Environment variables (token, owner, data dir,
                           owner's key, key secret)
requirements.txt           python-telegram-bot[job-queue] >= 21, cryptography
Dockerfile                 For Koyeb / any container host
sanctum-sanctorum.service  systemd unit for a VM/server
data/                      The SQLite index (vault.db), created at first run
tests/test_offline.py      Offline tests (no Telegram, no token)
tests/test_ai_offline.py   Offline tests for the persona + /chat (mocked AI)
tests/test_browse_offline.py  Offline tests for browse/wipe + menu commands
tests/test_fileid_offline.py  Offline tests for file_id storage, large-file
                           forwarding, the details fallback, and the DB
                           migration (no Telegram, no token)
tests/test_public_offline.py  Offline tests for the public bot: isolation,
                           migration to the owner, BYOK /chat routing, key
                           storage, per-user limits, /clear scoping, ban,
                           /admin (no Telegram, no token)
tests/test_audit_fixes.py  Offline tests for the security-audit fixes
tests/test_links_analyse.py  Offline tests for /links and /analyse:
                           per-user link storage, caps and URL validation,
                           bare-URL auto-save, BYOK refusal for /analyse,
                           image /analyse refusal (no vision call, no
                           download), text AI helpers, and the restored
                           menu commands (no Telegram, no token)
tests/test_per_user_numbering.py  Offline tests for per-user file
                           numbering (#1, #2… per vault) and its migration

tests/test_short_menu_offline.py  Offline tests for the short Menu:
                           exactly the 9 essentials (owner sees 13), all
                           26 commands still registered and working when
                           typed, filter buttons, /help Essentials + More
                           (no Telegram, no token)
tests/test_dedupe_offline.py  Offline tests for duplicate detection:
                           save-time "Already in your vault as #N",
                           the dedupe migration (13 files, 4 duplicate
                           pairs -> 9 files #1-#9), per-user isolation,
                           delete leaves a gap (next is MAX+1), the
                           download retry, and the /ask joke fix
                           (no Telegram, no token)

The Telegram Menu button shows only the 9 essentials — and it is
scoped: the DEFAULT menu (all private chats) lists `BOT_COMMANDS` in
bot.py (`/start`, `/help`, `/files`, `/search`, `/get`, `/stats`,
`/note`, `/links`, `/analyse`), while the owner's own menu adds
`/chat` (`KEEPER_COMMANDS`) and the owner-only commands
(`ADMIN_COMMANDS`: /admin, /users, /ban, /unban). Both menus are published on
startup via `set_my_commands`, so a non-owner never sees an
owner-only command. Every other implemented command (`/list`,
`/images`, `/videos`, `/audio`, `/docs`, `/apps`, `/delete`, `/clear`,
`/notes`, `/delnote`, `/dellink`, `/remind`, `/ask`, `/setkey`,
`/delkey`, `/mykey`) still works when typed — its
handler stays registered — and `/help` lists them all under "More".
`/files` and `/list` show filter buttons (All, Images, Videos, Audio,
Docs, Apps) that switch the view without typing a command. `/help`
shows the admin section to the keeper only.
```

## Upgrading from the owner-only bot

Opening a database created by the owner-only version runs a migration on
first start: `user_id` columns are added to the files and notes tables, and
every row that has no user id is assigned to the owner id in `OWNER_ID`
(they belonged to the owner). The migration is idempotent — re-opening
changes nothing, and rows that already have a user id are never touched.
No files are moved or lost; the old on-disk files keep serving from disk.

A later migration adds per-user file numbering (`user_num`): each user's
existing files are numbered #1, #2, … in id order, so the owner's old
global #3–#7 become his #1–#5 and another user's first file becomes
their #1. New files take max(user_num) + 1 for their owner only. Also
idempotent.

A Nov 15, 2026 migration removes duplicate rows: the same Telegram
file_id stored twice for one user (re-sending a file used to store a
second row with a new number). The earliest row is kept, the rest are
deleted, and that user's files are renumbered 1..N in id order, so the
vault reads gaplessly again. Legacy on-disk rows (empty file_id) are
never treated as duplicates. Idempotent — once clean, later opens are a
no-op. The live vault went from 13 files (4 duplicate pairs) to 9 files
#1–#9, originals keeping their numbers.

## Duplicates

Sending a file that is already in your vault stores nothing new: the bot
replies "Already in your vault as #N <name> (<category>, <size>)" and
tells you to use `/get N`. The match is the file's Telegram file_id, so
a renamed re-send still counts, and a duplicate costs no quota and no
daily-limit slot. The match is per-user: the same file in another user's
vault is not a duplicate. `/delete` does NOT renumber — a file's number
is a stable reference, so deleting #2 leaves a gap and the next new file
takes MAX+1 (#4), never #2. The migration above is the only renumbering,
a one-time cleanup.

## Full audit (Nov 15, 2026)

All 27 commands were exercised against a copy of the real vault (13
files, notes, links, owner + one friend, mocked AI, faked downloads):
/start, /help, /files, /images, /videos, /audio, /docs, /apps, /list,
/search, /get, /delete, /stats, /note, /notes, /delnote, /links,
/dellink, /remind, /ask, /chat, /setkey, /delkey, /mykey, /ocr,
/analyse, /clear — plus plain chat, bare-URL auto-save, group refusal,
and friend isolation. Result: 26 worked; 1 was broken — duplicate
saves (fixed above). Two rough edges were also fixed: /ask "tell me a
joke" fell through to the dry fallback while plain chat tells jokes
(/ask now routes the keeper's chitchat first), and a transient
download failure made /analyse fail on a tiny txt even though the
file was fine — downloads now retry (3 attempts, 0.5s/1.5s backoff).
Two reported mysteries turned out not to be bugs: file #12 was never
missing (it arrived at 19:29:56 IST, after the /files outputs that
said "11 total"), and there was no group leak — the bot refuses group
use, and the "group" preview was the bot DM itself.

Later the same day (Nov 15, 2026) the owner removed three things to
protect the free-tier host's 1 GB/month bandwidth allowance: /ocr was
deleted entirely, /analyse was restricted to text files and PDFs
(images are refused, no vision call), and /chat became keeper-only.
The bot now has 26 commands and a 9-item Menu (13 for the keeper).

## Limits (honest)

- Files are stored by Telegram file_id, so the bot never downloads anything.
  A bot can only **send** a file up to 50 MB. Files over 50 MB are sent back
  by forwarding the original message instead — proven live with a real
  110 MB APK. If the forward fails, the bot replies with the file's name,
  size, and the date it was sent, so you can find it in chat history. Files
  live on Telegram's servers, not on this machine, so a server wipe only
  loses the small index — not the files, though the bot would forget them
  without a backup of `vault.db`.
- Per-user limits: 500 files, 5 GB of reported sizes, 30 new files per day,
  500 notes, 500 saved links, 20 active reminders. The vault is per user and strictly isolated — one user can never see
  another's files or notes, and the owner has no access to other users'
  vaults (only aggregate totals via `/admin`). The index holds every user's
  file names, sizes, and dates in one database: the file bytes are on
  Telegram, but the metadata is on this server, so the server must be kept
  safe.
- The assistant is local and rule-based. It answers vault/notes questions,
  arithmetic, and date/time, and it chats in character — it is not a general
  AI and says so. `/chat` (keeper only) borrows a real AI mind on the
  keeper's key. The AI runs on Google's servers, not on this machine, and
  only `/chat` and `/analyse` ever send a message there.
  The owner's key is never spent on other users — each user brings their own.
- `/analyse` downloads the file to read it (Bot API limit 20 MB), extracts
  text from text files and PDFs (`pdftotext`, if installed), and sends at
  most the first 12,000 characters to Gemini; images are refused before
  any download; audio, video, archives, and unreadable files are answered
  from metadata only — never a guessed summary.
- Reminders live in memory (JobQueue). If the bot restarts, pending
  reminders are lost. Notes and the file index are in SQLite and survive
  restarts.
