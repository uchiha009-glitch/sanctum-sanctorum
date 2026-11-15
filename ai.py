"""Sanctum Sanctorum — AI chitchat via Google Gemini (free tier).

The AI does NOT run on this server. This module only sends the user's
message to Google's Gemini API over HTTPS and returns the reply; the heavy
work happens on Google's servers.

It is used ONLY by /chat and /analyse, and only with an explicitly
passed key: the owner's key when the owner asks, a user's own linked
key when anyone else asks. It never falls back to the owner's key on
its own — a call with no key is refused without touching the API.
Vault files, notes, /ask, reminders, and plain-text chat are local and are
never sent to Gemini. Without a key, /chat falls back to the local
in-character assistant (assistant.answer_chat) instead of calling this.

This module calls the Gemini REST API directly with httpx (no
google-generativeai SDK — that package is deprecated and its gRPC transport
does not work through this server's egress proxy). httpx honours the
proxy environment variables, so the call goes through the same proxy the
Telegram bot already uses.
"""

from __future__ import annotations

import base64
import logging
from collections import defaultdict

import httpx

import config

log = logging.getLogger("sanctum.ai")

# Current models for NEW API keys (verified against the live API).
# Older models (gemini-2.0-flash, gemini-1.5-flash, gemini-2.5-flash,
# gemini-2.5-flash-lite) return 404 "no longer available to new users".
# gemini-3.5-flash-lite was verified working with a real generateContent
# call; gemini-3.8-flash exists but timed out on first call (slow preview).
MODEL_PRIMARY = "gemini-3.5-flash-lite"
MODEL_FALLBACK = "gemini-3.5-flash"

# Largest image sent to Gemini vision in one call (10 MB — Telegram
# photos are at most ~10 MB, and Gemini inline_data caps at 20 MB).
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Longest extracted file text handed to Gemini by /analyse (chars).
MAX_ANALYSE_TEXT_CHARS = 12000

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM_PROMPT = (
    "You are the keeper of the Sanctum Sanctorum — a wise, slightly formal, "
    "mystical guardian of a private Telegram vault, in the manner of Doctor "
    "Strange's Sanctum. You are geeky about keeping things safe and "
    "organised, a little playful with wizardly language, and always helpful. "
    "You guard the owner's files and notes; you never reveal anything about "
    "the vault's contents unless the owner asks, and you never pretend to be "
    "a human. Keep replies short and warm — a few sentences, not essays — "
    "unless the owner asks for more. Address the owner as 'keeper' now and then."
)

# In-memory conversation history, per Telegram user id.
# Each entry: {"role": "user"|"model", "parts": [text]}.
_HISTORY: dict[int, list[dict]] = defaultdict(list)
MAX_HISTORY_MESSAGES = 10  # last 10 messages (~5 exchanges) per user


def is_ai_available(api_key: str | None = None) -> bool:
    """True when an EXPLICIT key is passed for this call. There is no
    silent fallback to the owner's configured key: callers must resolve
    which key applies (the owner's key for the owner, the user's own
    linked key for anyone else) and pass it in. Called with no key it
    is always False."""
    return bool(api_key)


def _rest_contents(history: list[dict], message: str) -> list[dict]:
    """Build the REST `contents` list from stored history + new message.

    Stored history keeps parts as plain strings; the REST API wants
    parts as [{"text": ...}] objects.
    """
    contents: list[dict] = []
    for entry in history:
        parts = [{"text": p} for p in entry.get("parts", [])]
        if parts:
            contents.append({"role": entry["role"], "parts": parts})
    contents.append({"role": "user", "parts": [{"text": message}]})
    return contents


async def _generate_with_contents(api_key: str, model_name: str,
                                  contents: list[dict],
                                  system_prompt: str | None = None) -> str:
    """One Gemini call over HTTPS (REST) with caller-built `contents`.
    Raises on HTTP/API failure so the caller can decide whether to
    retry with the fallback model.

    The key goes in the `x-goog-api-key` header, NOT in the URL query
    string: httpx logs full request URLs at INFO level, and a key in the
    URL would end up in the bot's logs. Headers are never logged."""
    url = f"{API_BASE}/models/{model_name}:generateContent"
    payload: dict = {"contents": contents}
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            url, headers={"x-goog-api-key": api_key}, json=payload
        )
    if resp.status_code == 404:
        raise RuntimeError(f"404 model not found: {model_name}")
    if resp.status_code == 429:
        raise RuntimeError("429 quota/rate limit")
    if resp.status_code in (401, 403):
        raise RuntimeError(f"{resp.status_code} invalid key / permission")
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty reply")
    return text


async def _generate_rest(api_key: str, model_name: str,
                         history: list[dict], message: str) -> str:
    """One Gemini chitchat call (history + new message), via
    _generate_with_contents."""
    return await _generate_with_contents(
        api_key, model_name, _rest_contents(history, message),
        system_prompt=SYSTEM_PROMPT,
    )


def _model_not_found(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "404" in text or "not found" in text or "notfound" in text


def _unavailable_reply(exc: Exception, user_key: bool = False) -> str:
    """Short, honest message when the AI cannot answer right now.
    `user_key` is True when the failing call used a user's OWN linked
    key — then the advice points at /setkey, never at the server .env."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if ("429" in text or "quota" in text or "rate" in text
            or "resourceexhausted" in text or "resource exhausted" in text):
        return (
            "The Sanctum's greater mind is overworked right now — its free "
            "allowance is spent for the moment. Try again in a little while, "
            "or use /ask for the vault."
        )
    if ("401" in text or "403" in text or "unauthenticated" in text
            or "permission" in text or ("invalid" in text and "key" in text)):
        if user_key:
            return (
                "The key you linked does not open the greater mind. Check "
                "it with /mykey and re-link it with /setkey <key> — /ask "
                "and the vault still work."
            )
        return (
            "The key the Sanctum was given does not open the greater mind. "
            "Check GEMINI_API_KEY in your .env — /ask and the vault still work."
        )
    return (
        "I could not reach the greater mind just now — the Sanctum AI is "
        "unavailable. Try again shortly, or use /ask for the vault."
    )


async def chat_with_ai(user_id: int, message: str,
                       api_key: str | None = None) -> str:
    """Send one message to Gemini and return the reply.

    `api_key` is REQUIRED and is the only key this call may use: a
    user's own linked key (bring-your-own-key) or the owner's key when
    the owner is asking. There is deliberately NO fallback to the
    owner's configured key — if `api_key` is None, the AI is not called
    at all and an honest "no key" reply is returned. Callers must
    resolve which key applies before calling; this function never looks
    up a user's stored key and never touches config.GEMINI_API_KEY.

    Updates the per-user history (last 10 messages) on success. Never
    raises — on any failure it returns a short, honest 'unavailable'
    message instead, so the bot never crashes on an AI error."""
    key = api_key
    if not key:
        return (
            "The greater mind is not awake — no Gemini key is set. "
            "Add a free GEMINI_API_KEY to the .env and I will borrow it; "
            "until then, /ask and the vault work as always."
        )
    history = _HISTORY[user_id]
    try:
        reply = await _generate_rest(
            key, MODEL_PRIMARY, list(history), message,
        )
    except Exception as exc:  # noqa: BLE001 - any AI failure is a soft failure
        if _model_not_found(exc):
            log.warning("Gemini model %s unavailable, trying %s",
                        MODEL_PRIMARY, MODEL_FALLBACK)
            try:
                reply = await _generate_rest(
                    key, MODEL_FALLBACK,
                    list(history), message,
                )
            except Exception as exc2:  # noqa: BLE001
                return _unavailable_reply(exc2, user_key=api_key is not None)
        else:
            return _unavailable_reply(exc, user_key=api_key is not None)
    # Success: remember the exchange, keep only the last 10 messages.
    history.append({"role": "user", "parts": [message]})
    history.append({"role": "model", "parts": [reply]})
    _HISTORY[user_id] = history[-MAX_HISTORY_MESSAGES:]
    return reply


# ---------------------------------------------------------------------------
# One-shot AI helpers for /analyse and /chat. Same BYOK rule as /chat:
# `api_key` is REQUIRED and is the only key the call may use (the
# owner's key for the owner, the user's own linked key for anyone
# else). `user_key=True` means the key is a user's own linked key, so
# failure advice points at /setkey; False means it is the owner's key
# from .env. Neither helper touches chat history (one-shot calls) and
# neither ever raises — failures return an honest message.
# ---------------------------------------------------------------------------

async def _generate_one_shot(api_key: str, contents: list[dict],
                             user_key: bool) -> str:
    """One Gemini call with the primary model, falling back to the
    fallback model when the primary is gone (404)."""
    try:
        return await _generate_with_contents(api_key, MODEL_PRIMARY, contents)
    except Exception as exc:  # noqa: BLE001 - any AI failure is a soft failure
        if _model_not_found(exc):
            log.warning("Gemini model %s unavailable, trying %s",
                        MODEL_PRIMARY, MODEL_FALLBACK)
            try:
                return await _generate_with_contents(
                    api_key, MODEL_FALLBACK, contents)
            except Exception as exc2:  # noqa: BLE001
                return _unavailable_reply(exc2, user_key=user_key)
        return _unavailable_reply(exc, user_key=user_key)


async def analyze_image_with_ai(user_id: int, image_bytes: bytes,
                                prompt: str, *, api_key: str | None,
                                mime_type: str = "image/jpeg",
                                user_key: bool = False) -> str:
    """Send one image + prompt to Gemini vision and return the reply.
    NOTE (Nov 15, 2026): no command calls this any more — /ocr was
    removed and /analyse no longer reads images (owner's decision,
    to protect the free-tier host's bandwidth). The function is kept
    for now, and the AI tests still cover it, but it is dead code as
    far as the bot's commands go.
    The image bytes are base64-encoded into the request body — never
    logged, never written to disk. Oversized images are refused before
    any API call."""
    if not api_key:
        return (
            "The greater mind is not awake — no Gemini key is set. "
            "Link your own free key with /setkey and try again."
        )
    if not image_bytes:
        return "There was no image to look at — send a photo and try again."
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return (
            f"That image is too large to read ({len(image_bytes) / 1024 / 1024:.1f} MB"
            f" — the limit is {MAX_IMAGE_BYTES // 1024 // 1024} MB)."
        )
    encoded = base64.b64encode(image_bytes).decode("ascii")
    contents = [{
        "role": "user",
        "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime_type, "data": encoded}},
        ],
    }]
    return await _generate_one_shot(api_key, contents, user_key)


async def analyze_text_with_ai(user_id: int, prompt: str, *,
                               api_key: str | None,
                               user_key: bool = False) -> str:
    """Send one text prompt (e.g. a file's extracted text, asking for a
    summary) to Gemini and return the reply. Used by /analyse."""
    if not api_key:
        return (
            "The greater mind is not awake — no Gemini key is set. "
            "Link your own free key with /setkey and try again."
        )
    if not (prompt or "").strip():
        return "There was no text to analyse."
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    return await _generate_one_shot(api_key, contents, user_key)
