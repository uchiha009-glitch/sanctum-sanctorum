# Sanctum Sanctorum — launch pack

Status on 2026-11-15, 20:30 IST: **DO NOT POST YET.**
Rebuilt as the new bot **@SanctumVaultBot** (https://t.me/SanctumVaultBot)
after @IG79Tbot was deleted. Same code, all 12 audit fixes. The
/ocr removal, /analyse text-and-PDF-only limit, owner-only /chat
AND owner-only /users (list users + last seen) are all LIVE and
verified: 246/246 offline tests pass, service PID 10563 since
20:26 IST, deploy ZIP rebuilt (121,520 bytes). Menu: 9 entries for
everyone, 14 for the owner (9 + /chat, /admin, /users, /ban,
/unban). Note: both current users show last seen "—" until they
next interact — tracking started with this restart.
The last gate is the live two-account test below, run fresh on the
NEW bot — the old bot's test does not carry over. Post only after
it passes.

## Part 1 — Live two-account isolation test (5 minutes)

You need a second real Telegram account: a second number of yours, or a
friend. Doom cannot invent one. The friend who tested the old bot
(user 6428830709) already has 3 files and 1 note on the new bot, so
use a file with a unique name for this test — do not rely on an
empty vault on either side.

**Setup — from your account (owner, @SanctumVaultBot):**
1. Send the bot a file with a unique name, e.g. `sanctum-isolation-test.txt`
   (any small text file, content: "only for Aseem").
2. Send `/files` and note its exact name / number. (Per-user numbering:
   each user starts at #1 in their own vault.)

**From the second account:**
3. Open @SanctumVaultBot, send `/start`, then `/files`.
   Expected: only the friend's own files — your test file is NOT there.
4. Send `/get sanctum-isolation-test.txt` (or `/search sanctum-isolation-test`).
   Expected: "not found" — no name, no file.
5. Send the bot its own file, e.g. `sanctum-isolation-test-friend.txt`.
6. Send `/files`. Expected: the friend's own files plus their new
   test file — none of your files.

**Back on your account:**
7. Send `/files`. Expected: your files only — the friend's file is NOT there.
8. Send `/get sanctum-isolation-test-friend.txt`.
   Expected: "not found".

**Pass = all 8 expectations hold. Fail = any file crosses over.**
If it passes, tell Doom and the Reddit post below is cleared to go.
If anything crosses over, do NOT post — tell Doom exactly which step
showed the wrong file.

Optional extra check (already fixed in code, quick to confirm live):
`/ask 9**9**9` — expected: refused immediately, bot stays responsive.

## Part 2 — Reddit post (post ONLY after Part 1 passes)

- Subreddit: r/TelegramBots (bot showcase — check its sidebar/rules and
  flair before posting; self-promotion rules were not verified in advance)
- Bot: @SanctumVaultBot — https://t.me/SanctumVaultBot
- Title: `Sanctum Sanctorum — a free Telegram file vault where every user gets their own private vault`

### Post body (copy as-is)

I built Sanctum Sanctorum, a free Telegram bot that works as a personal
file vault. Send it any file and it keeps it for you — send `/get` to
get it back.

**How it works:** the bot stores only the file's Telegram ID, name, size
and date — Telegram holds the file itself, nothing is downloaded to a
server, so there is no 20 MB download limit. Files up to 50 MB come back
by file ID; larger files come back by forwarding the original message
(tested live with a 110 MB APK). If that forward ever fails, it gives you
the file's name, size and exact date so you can find it in chat history.

**Commands:** menu is short on purpose — `/files` (with All/Images/
Videos/Audio/Docs/Apps filter buttons) `/search` `/get` `/stats`
`/note` `/links` `/analyse`, plus `/start` and `/help`. The keeper's
own menu adds `/chat`, `/admin`, `/users`, `/ban`, `/unban`.
Everything else still works when typed: `/list`
`/images` `/videos` `/audio` `/docs` `/apps` `/delete` `/clear`
`/notes` `/delnote` `/dellink` `/remind` `/ask` `/setkey` `/delkey`
`/mykey`. `/analyse` summarises a vault text file or PDF (images are
refused); `/links` saves links (a bare URL auto-saves).

**Privacy:** every vault is locked to one Telegram user ID. You can never
see, search, get or delete another user's files, notes or links — and
neither can I as the owner. I can only see who uses the bot via
`/users` (name, first seen, last seen, and file/note/link counts) and
aggregate totals via `/admin`. Keys for the optional AI chat are
encrypted at rest.

**Limits, honestly:** 500 files, 5 GB total, 30 new files/day, 500 notes,
500 links, 20 reminders per user. The bot only works in private chats,
not groups. AI chat (`/chat`) is the keeper's alone. `/analyse` is
optional and bring-your-own-key: link your own free Gemini key with
`/setkey` — my key is never spent on anyone else, and without a key
the vault works fully without AI.

**Honest limits:** the file index (names, sizes, dates — not the files
themselves) lives in one database on the server, and reminders are lost
if the bot restarts. It is free, running now at @SanctumVaultBot.

Try it and tell me what breaks — that is what I want to hear.

### After posting
- Watch the first replies for abuse reports / broken `/get` on large files.
- Do not share the deployable ZIP publicly until the hosting decision is
  made (Google Cloud e2-micro free tier chosen; Oracle dropped, Fly.io
  has no free tier for new accounts; the VM is not created yet).
- If the bot gets flooded, per-user limits and `/ban` are the controls;
  tell Doom and the limits can be tightened.
