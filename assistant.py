"""Sanctum Sanctorum — local assistant (no external AI, no API keys).

Everything here is rule-based and runs entirely on the server:
  * notes are stored in the vault database,
  * reminders are scheduled through python-telegram-bot's JobQueue,
  * /ask answers questions about the vault, does safe arithmetic, tells the
    date/time, and is honest about what it cannot do,
  * plain chat (and /chat without a Gemini key) is answered in character
    by the Sanctum's keeper — a geeky wizard guardian, a little mystical,
    proud of keeping the vault safe.

No message content ever leaves the machine. The external Gemini AI is a
separate module (ai.py) and is only ever called by the /chat command.
"""

from __future__ import annotations

import ast
import math
import re
from datetime import datetime

from vault import CATEGORY_LABELS, Store, human_size

# ---------------------------------------------------------------------------
# Safe arithmetic: only numbers and + - * / % ** // and parentheses.
# A single message must never wedge the bot: the evaluator below refuses
# any computation whose result would exceed _MAX_ARITH_BITS *before* the
# big-int work happens (e.g. 9**9**9 is refused by a bit estimate, not
# by computing a ~1.2-billion-bit number first). It never uses eval().
# ---------------------------------------------------------------------------

_MAX_ARITH_BITS = 4096   # largest integer result allowed (in bits)
_MAX_ARITH_EXPR_LEN = 200  # longest expression accepted (characters)

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    ast.FloorDiv,
    ast.USub,
    ast.UAdd,
    ast.Constant,
)


def _check_result(result) -> int | float:
    """Refuse results that are not plain finite numbers of sane size."""
    if isinstance(result, bool):  # bool is an int subclass — never valid here
        raise ValueError("not a number")
    if isinstance(result, int):
        if abs(result).bit_length() > _MAX_ARITH_BITS:
            raise ValueError("that number is too large to compute")
        return result
    if isinstance(result, float):
        if not math.isfinite(result):
            raise ValueError("that number is too large to compute")
        return result
    raise ValueError("not a number")


def _safe_pow(base, exp) -> int | float:
    """Power with a pre-check: estimate the result's size in bits and
    refuse before computing if it would exceed _MAX_ARITH_BITS.
    This catches 9**9**9 (the inner 9**9 is 387420489, so the outer power
    would be ~1.2 billion bits) without ever doing the giant multiply.
    ZeroDivisionError (0**-1) propagates, as callers expect."""
    if isinstance(exp, int) and exp >= 0 and base not in (0, 1, -1):
        if isinstance(base, int):
            # log2(base) is between bit_length-1 and bit_length; use the
            # lower bound so the estimate never over-refuses by much, and
            # _check_result enforces the exact cap on the real result.
            est_bits = max(1, abs(base).bit_length() - 1) * exp
        else:
            est_bits = abs(math.log2(abs(base))) * exp if base else 0
        if est_bits > _MAX_ARITH_BITS:
            raise ValueError("that power is too large to compute")
    return _check_result(base ** exp)


def _eval_node(node: ast.AST) -> int | float:
    """Recursively evaluate one AST node. Only plain arithmetic is
    reachable here — the caller has already rejected every other node
    type. Raises ValueError on anything too large to compute, and lets
    ZeroDivisionError propagate (callers turn it into a polite reply)."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise ValueError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand)
        if isinstance(node.op, ast.USub):
            return _check_result(-value)
        if isinstance(node.op, ast.UAdd):
            return _check_result(+value)
        raise ValueError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        op = node.op
        if isinstance(op, ast.Pow):
            return _safe_pow(left, right)
        if isinstance(op, ast.Add):
            return _check_result(left + right)
        if isinstance(op, ast.Sub):
            return _check_result(left - right)
        if isinstance(op, ast.Mult):
            return _check_result(left * right)
        if isinstance(op, ast.Div):
            return _check_result(left / right)
        if isinstance(op, ast.FloorDiv):
            return _check_result(left // right)
        if isinstance(op, ast.Mod):
            return _check_result(left % right)
        raise ValueError("unsupported operator")
    raise ValueError("not a plain arithmetic expression")


def safe_eval(expr: str) -> float | int:
    """Evaluate a plain arithmetic expression. Raises ValueError on anything
    that is not pure arithmetic (no names, calls, attributes, imports),
    and on any computation whose result would be unreasonably large.
    Never uses eval() — the expression is walked node by node."""
    if len(expr) > _MAX_ARITH_EXPR_LEN:
        raise ValueError("expression too long")
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError("not a plain arithmetic expression")
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, (int, float)
        ):
            raise ValueError("only numbers are allowed")
    try:
        result = _eval_node(tree)
    except OverflowError:
        # e.g. int/int division whose quotient overflows a float.
        raise ValueError("that number is too large to compute") from None
    if not isinstance(result, (int, float)):
        raise ValueError("not a number")
    return result


_ARITH_RE = re.compile(r"^[\d\s\.\+\-\*\/\%\(\)]+$")


def _try_arithmetic(question: str) -> str | None:
    """Return an answer if the question is (or contains) arithmetic."""
    q = question.strip()
    lowered = q.lower()
    for prefix in ("calculate ", "calc ", "what is ", "what's ", "compute ", "solve "):
        if lowered.startswith(prefix):
            q = q[len(prefix):].strip()
            lowered = q.lower()
            break
    q = q.rstrip("?").strip()
    if _ARITH_RE.match(q) and any(ch.isdigit() for ch in q) and any(
        op in q for op in "+-*/%"
    ):
        try:
            return f"{q} = {safe_eval(q)}"
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
            return "That arithmetic didn't work — check the expression and try again."
    return None


# ---------------------------------------------------------------------------
# /ask — rule-based answers about the vault, notes, time, and capabilities.
# ---------------------------------------------------------------------------

HELP_BLURB = (
    "I am the keeper of the Sanctum Sanctorum — guardian of your vault, not "
    "a distant mind. I have no external AI; I run entirely on this server, "
    "and I can help with:\n"
    "• your vault — /list, /search, /get, /delete, /stats\n"
    "• notes — /note, /notes, /delnote\n"
    "• reminders — /remind <minutes> <text>\n"
    "• simple arithmetic, and the date/time.\n"
    "For deeper conversation, use /chat <message> — with a free Gemini key "
    "I can borrow a greater mind; without one, my own local wit will have "
    "to do.\n"
    "Ask me things like 'how many files do I have?', 'what notes do I have "
    "about exams?', 'what is 45 * 12?' — or just say 'tell me a joke'."
)


def _answer_ask_inner(question: str, store: Store, user_id: int) -> str | None:
    """Answer a question with local rules only. Returns None when no rule
    matches, so callers can choose their own fallback. Never invents facts.
    Every vault/notes lookup is scoped to `user_id` — the caller only
    ever sees their own vault."""
    q = (question or "").strip()
    if not q:
        return HELP_BLURB
    lowered = q.lower()

    # Arithmetic first.
    arith = _try_arithmetic(q)
    if arith is not None:
        return arith

    # Date / time.
    if any(p in lowered for p in ("what time", "what's the time", "current time", "time is it")):
        return "It is " + datetime.now().astimezone().strftime("%A, %d %B %Y, %H:%M %Z") + " (server local time)."
    if any(p in lowered for p in ("what date", "what's the date", "what day is it", "today's date", "todays date")):
        return "Today is " + datetime.now().astimezone().strftime("%A, %d %B %Y") + "."

    # Identity / capabilities.
    if any(p in lowered for p in ("who are you", "what are you", "your name")):
        return (
            "I am the Sanctum Sanctorum — keeper of this vault, guardian of "
            "your files and notes. I dwell on this server alone: no cloud, "
            "no external mind, only the wards I keep. What you entrust to me, "
            "I keep safe."
        )
    if any(p in lowered for p in ("what can you do", "help me", "your abilities", "commands")):
        return HELP_BLURB

    # Vault questions.
    if "how many files" in lowered or ("count" in lowered and "file" in lowered):
        s = store.stats(user_id=user_id)
        lines = [
            f"You have {s['total_files']} file(s) stored, "
            f"{human_size(s['total_size'])} in total."
        ]
        for cat, info in sorted(s["by_category"].items()):
            label = CATEGORY_LABELS.get(cat, cat)
            lines.append(f"• {label}: {info['count']} file(s), {human_size(info['size'])}")
        return "\n".join(lines)
    if any(p in lowered for p in ("what files", "list my files", "list files", "show my files", "my files")):
        files, total = store.list_files(limit=10, user_id=user_id)
        if total == 0:
            return "Your vault is empty. Send me any file and I will keep it safe."
        lines = [f"Your {total} file(s) — newest first (showing {len(files)}):"]
        lines += [f"• {f.display()}" for f in files]
        if total > len(files):
            lines.append("Use /list to page through them all.")
        return "\n".join(lines)

    # Notes questions.
    if "note" in lowered:
        m = re.search(r"notes?\b.*?\b(?:about|on|for)\s+(.+)", lowered)
        if m:
            topic = m.group(1).strip(" ?.")
            notes = store.search_notes(topic, user_id=user_id)
            if not notes:
                return f"No notes about '{topic}'."
            return "Notes about '" + topic + "':\n" + "\n".join(
                f"• #{n.id}: {n.text[:120]}" for n in notes
            )
        if any(p in lowered for p in ("what notes", "list notes", "my notes", "show notes", "all notes")):
            notes, total = store.list_notes(limit=10, user_id=user_id)
            if total == 0:
                return "You have no notes yet. Save one with /note <text>."
            lines = [f"Your {total} note(s) — newest first (showing {len(notes)}):"]
            lines += [f"• #{n.id}: {n.text[:120]}" for n in notes]
            return "\n".join(lines)

    # Search the vault by topic.
    m = re.search(r"(?:find|search|look for|get)\s+(?:me\s+)?(.+)", lowered)
    if m and "file" in lowered:
        topic = m.group(1).strip(" ?.")
        files = store.search_files(topic, user_id=user_id)
        if not files:
            return f"Nothing in the vault matches '{topic}'."
        return "Matches for '" + topic + "':\n" + "\n".join(
            f"• {f.display()}" for f in files
        ) + "\nUse /get <id> to download one."

    # Nothing matched — the caller chooses the fallback.
    return None


def answer_ask(question: str, store: Store, user_id: int) -> str:
    """Answer a question with local rules only (the /ask command)."""
    # The keeper's chitchat answers here too — /ask "tell me a joke"
    # used to fall through to the dry fallback even though plain chat
    # tells jokes (and HELP_BLURB invites asking for one). Same rules
    # as answer_chat: chitchat first, then vault/arithmetic/date, then
    # the honest fallback.
    chitchat = _try_chitchat((question or "").strip().lower())
    if chitchat is not None:
        return chitchat
    answer = _answer_ask_inner(question, store, user_id)
    if answer is not None:
        return answer
    # Honest fallback — never pretend to be an external AI — but the
    # keeper may still be playful about it.
    return (
        "That riddle is beyond my local wards — I have no external AI, only "
        "local rules and my own small wit. I can help with your vault "
        "(/list, /search, /get, /stats), notes (/note, /notes), reminders "
        "(/remind <minutes> <text>), simple arithmetic, and the date/time — "
        "or, if you fancy a laugh, just say 'tell me a joke'. Try asking "
        "one of those."
    )


# ---------------------------------------------------------------------------
# Sanctum persona — basic chitchat, in character.
# Used for plain-text messages and for /chat when no Gemini key is set.
# The keeper is a geeky wizard guardian: a little mystical, warm, and proud
# of keeping the vault safe. Replies are short and always in character.
# ---------------------------------------------------------------------------

def _try_chitchat(lowered: str) -> str | None:
    """Return an in-character reply for simple chat, or None."""
    msg = lowered.strip(" .!?")

    # Greetings.
    if msg in ("hello", "hi", "hey", "yo", "hii", "hiii", "namaste",
               "hello there", "hi there", "hey there", "good day") or \
            msg.startswith(("hello ", "hi ", "hey ", "yo ", "namaste ")):
        return (
            "Greetings, keeper. The Sanctum is awake, the wards are lit, and "
            "your vault stands guarded. What do you need?"
        )
    if msg.startswith("good morning"):
        return (
            "Good morning, keeper. The Sanctum stirs with the dawn — your "
            "vault is safe and the shelves are in order. What shall we do today?"
        )
    if msg.startswith("good afternoon"):
        return (
            "Good afternoon, keeper. The Sanctum keeps its quiet watch — your "
            "files are safe. What do you need?"
        )
    if msg.startswith("good evening"):
        return (
            "Good evening, keeper. The candles are lit and the vault is sealed "
            "tight. What brings you to the Sanctum?"
        )

    # Farewells.
    if msg in ("bye", "goodbye", "good bye", "see you", "see ya", "good night",
               "goodnight", "good night keeper"):
        return (
            "Farewell, keeper. The Sanctum will keep watch over your vault "
            "until you return — it never sleeps."
        )

    # How are you.
    if any(p in lowered for p in ("how are you", "how're you", "how are u",
                                  "how r u", "how's it going", "how is it going",
                                  "how are things")):
        return (
            "The wards hold, the shelves are dusted, and not a single file has "
            "gone missing — I am well, thank you for asking. How fares your "
            "day, keeper?"
        )

    # Thanks.
    if "thank" in lowered:
        return (
            "You are most welcome. What you entrust to the Sanctum, the "
            "Sanctum keeps. Is there anything else, keeper?"
        )

    # Jokes — the keeper's own small book of jokes, no AI involved.
    # They rotate in order, so every request gets a fresh one.
    if any(p in lowered for p in ("tell me a joke", "tell a joke",
                                  "make me laugh", "something funny",
                                  "say something funny", "joke please",
                                  "a joke")) or msg == "joke":
        return _next_joke()

    # Laughing along — the keeper enjoys a good laugh too.
    if any(p in lowered for p in ("haha", "lol", "lmao", "rofl", "hehe",
                                  "that was funny", "that was a good joke",
                                  "you're funny", "you are funny", "so funny",
                                  "made me laugh", "that made me laugh")):
        return (
            "Ha! The keeper lands one. Careful, keeper — laugh too hard and "
            "you will drop your files. Do not worry, I will catch them. "
            "I catch everything."
        )

    # Being teased — the keeper takes it in good humour.
    if any(p in lowered for p in ("you're dumb", "you are dumb", "dumb bot",
                                  "stupid bot", "you're stupid",
                                  "you are stupid", "useless bot",
                                  "you're useless", "you are useless",
                                  "bad bot")):
        return (
            "Dumb? I have guarded this vault without losing a single file, "
            "remembered every note, and I tell jokes on demand. If that is "
            "dumb, then the bar is set very high indeed. Try me: say "
            "'tell me a joke', or ask how many files you have."
        )

    # Bored — offer something to do.
    if "bored" in lowered:
        return (
            "Bored, keeper? Then let us have some fun — say 'tell me a joke', "
            "or send me a file to guard, or ask me how many files you have. "
            "The vault is never boring, only patient."
        )

    # Bare "help" typed as chat.
    if msg == "help":
        return HELP_BLURB

    return None


# The keeper's own jokes — written here, told from memory, never fetched
# from any AI. Rotated in order so repeat requests stay fresh.
_JOKES = (
    "I told the vault a joke about a missing file once. It did not laugh — "
    "it just filed it under 'unsolved mysteries'.",
    "Why do wizards make terrible librarians? Every time you ask for a book, "
    "they say 'it is in a vault only YOU can open'.",
    "A traveller once asked me for a joke. I gave him one. He laughed so "
    "hard he nearly dropped his APK — do not worry, I caught it. "
    "I catch everything.",
    "Why did the keeper cross the server? To get to the other side of the "
    "firewall.",
    "My crystal ball told me a joke yesterday. I cannot repeat it — it is "
    "classified. Even I do not know what it means.",
    "What do you call a wizard who loses your files? Unemployed. Luckily, "
    "that has never happened here.",
)

_JOKE_INDEX = 0


def _next_joke() -> str:
    """The next joke in rotation (wraps around). Module-level state on
    purpose: the rotation is per-process, and the jokes are fixed text."""
    global _JOKE_INDEX
    joke = _JOKES[_JOKE_INDEX % len(_JOKES)]
    _JOKE_INDEX += 1
    return joke


CHAT_POINTER = (
    "Hmm — that one slips past my wards, and my crystal ball is off duty. "
    "I am a keeper, not a mind-reader. For all I can do, see /help. Or ask "
    "me about your vault, your notes, or a little arithmetic — or say "
    "'tell me a joke' and I shall do my best."
)

# The keeper's own pointer: /chat is keeper-only, so only the keeper is
# ever pointed at it. Everyone else gets CHAT_POINTER above, which never
# mentions /chat.
CHAT_POINTER_OWNER = (
    "Hmm — that one slips past my wards, and my crystal ball is off duty. "
    "I am a keeper, not a mind-reader. For deeper conversation, speak with "
    "/chat <message>; for all I can do, see /help. Or ask me about your "
    "vault, your notes, or a little arithmetic — or say 'tell me a joke' "
    "and I shall do my best."
)


def answer_chat(message: str, store: Store, user_id: int,
                is_owner: bool = False) -> str:
    """In-character local reply for plain chat (and /chat without a key).
    Never empty, never silent: chitchat first, then the /ask rules (vault,
    arithmetic, date/time), then an in-character pointer. Nothing here ever
    calls an external AI. The pointer never mentions /chat to a non-owner,
    since /chat answers only its keeper."""
    msg = (message or "").strip()
    if not msg:
        if is_owner:
            return (
                "Speak, keeper — I am listening. A greeting, a question about "
                "your vault, or /chat <message> for deeper conversation."
            )
        return (
            "Speak, keeper — I am listening. A greeting, a question about "
            "your vault, or /help to see all I can do."
        )
    lowered = msg.lower()
    chitchat = _try_chitchat(lowered)
    if chitchat is not None:
        return chitchat
    answer = _answer_ask_inner(msg, store, user_id)
    if answer is not None:
        return answer
    return CHAT_POINTER_OWNER if is_owner else CHAT_POINTER


# ---------------------------------------------------------------------------
# Reminders (JobQueue).
# ---------------------------------------------------------------------------

async def reminder_callback(context) -> None:
    """Fired by the JobQueue: deliver the reminder to the owner."""
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    if chat_id:
        await context.bot.send_message(
            chat_id=chat_id, text=f"⏰ Reminder: {text}"
        )


def schedule_reminder(job_queue, chat_id: int, minutes: int, text: str):
    """Schedule a one-off reminder. Returns the Job object."""
    return job_queue.run_once(
        reminder_callback,
        when=minutes * 60,
        data={"chat_id": chat_id, "text": text},
        name=f"remind-{chat_id}-{minutes}",
      )
