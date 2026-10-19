"""Automatic translation of admin custom templates into all supported languages.

Admin uploads a template in ANY supported language (English, Bengali, etc.) and
the bot translates it into every other supported language automatically (Google
Translate free endpoint, no API key required). The language used to upload the
template determines the source language for translation, and the uploaded text
is stored verbatim in that language. Translations are cached per (template,
language) keyed by the MD5 of the source text, so:

- a template is only ever translated once per language and stays stable across
  restarts;
- manual per-language edits made by the admin are respected and never
  overwritten;
- changing the source re-translates the auto languages on the next save.

Placeholders (``{phone}`` etc.), URLs and @handles are masked before the request
and restored afterwards so the translator can never mangle them.
"""
import hashlib
import logging
import re
import requests
import threading
import time
import unicodedata
import urllib.parse

from bot.data.languages import DEFAULT_LANGUAGE, LANGUAGES
from bot.data.templates import set_message_template
from bot.data.state import _storage, save_storage

logger = logging.getLogger(__name__)

# Our language code -> Google Translate language code.
GOOGLE_LANG_MAP = {
    "en": "en",
    "ur": "ur",
    "ar": "ar",
    "hi": "hi",
    "bn": "bn",
    "id": "id",
    "es": "es",
    "ru": "ru",
    "zh_cn": "zh-CN",
    "zh_tw": "zh-TW",
    "ja": "ja",
    "ko": "ko",
    "my": "my",
}

# Google free-endpoint variants tried in order. The ``client`` value picks a
# different rate-limit bucket: ``dict-chrome-ex`` (used by Chrome's translate)
# frequently keeps working when ``gtx`` is throttled on the same IP.
_GOOGLE_ENDPOINTS = [
    ("https://translate.googleapis.com/translate_a/single", "dict-chrome-ex"),
    ("https://translate.googleapis.com/translate_a/single", "gtx"),
    ("https://translate.google.com/translate_a/single", "gtx"),
]
_MYMEMORY_URL = "https://api.mymemory.translated.net/get"
_REQUEST_TIMEOUT = 20
_MAX_CHUNK = 1200
_MAX_RETRIES = 1

# The free Google endpoint rate-limits aggressively per IP: hammering it with
# back-to-back requests makes it return HTTP 429 for everything. A small
# mandatory gap between requests (shared by every caller, thread-safe) keeps it
# within the limit. When Google still 429s, a circuit breaker opens so the rest
# of the batch skips Google entirely and uses the fallback provider instead of
# crawling through long backoff sleeps for every language.
_MIN_GOOGLE_INTERVAL = 2.0
_GOOGLE_BREAKER_COOLDOWN = 60.0
_google_last_ts = 0.0
_google_throttle_lock = threading.RLock()
_google_breaker_until = 0.0
_google_breaker_lock = threading.RLock()


def _throttle_google() -> None:
    """Ensure at least ``_MIN_GOOGLE_INTERVAL`` seconds between Google requests."""
    global _google_last_ts
    with _google_throttle_lock:
        now = time.monotonic()
        wait = _MIN_GOOGLE_INTERVAL - (now - _google_last_ts)
        if wait > 0:
            time.sleep(wait)
        _google_last_ts = time.monotonic()


def _google_breaker_open() -> bool:
    with _google_breaker_lock:
        return time.monotonic() < _google_breaker_until


def _open_google_breaker() -> None:
    global _google_breaker_until
    with _google_breaker_lock:
        _google_breaker_until = time.monotonic() + _GOOGLE_BREAKER_COOLDOWN

# Single-pass mask so inserted ``@@T<n>@@`` tokens are never re-scanned by a
# later regex (an @-regex would otherwise match the ``@T0`` inside a token).
_MASK_RE = re.compile(r"https?://[^\s]+|@[a-zA-Z0-9_]+|\{[a-zA-Z0-9_]+\}")

# Tolerant restore: MyMemory sometimes mangles ``@@T0@@`` into ``@T0@@``,
# ``@@T0 @@``, ``@ @ T0 @ @``, ``@@ T 0 @@`` or even ``T0@@`` (no leading @).
# This pattern matches all of those variants so placeholders, URLs and @handles
# are still restored exactly.
_TOKEN_RESTORE_RE = re.compile(r"(?:@\s*){0,4}T\s*(\d+)(?:\s*@){1,4}")

# Leftover token-like garbage that survived translation (e.g. ``@T0@@`` or
# ``T0@@``) means a stored translation is mangled and must be re-translated.
_MANGLED_TOKEN_RE = re.compile(r"@\s*T\s*\d|T\s*\d+\s*@")

# Unicode "decorated" letters that Google Translate cannot process: mathematical
# alphanumeric symbols (U+1D400..U+1D7FF), fullwidth A-Z/a-z/0-9
# (U+FF10..U+FF19, U+FF21..U+FF3A, U+FF41..U+FF5A) and letter-like roman
# numerals (U+2160..U+217F). Styled English such as ``"𝚆𝚎𝚕𝚌𝚘𝚖𝚎"`` is passed
# through untranslated (or mangled) by the free endpoint, so we normalize it to
# plain ASCII before translating and refuse to store any result that still
# carries such characters. Fullwidth punctuation (e.g. U+FF0B) is excluded as it
# is legitimate in CJK text.
_STYLED_CHARS_RE = re.compile(
    r"[\U0001d400-\U0001d7ff"
    r"\U0000ff10-\U0000ff19"
    r"\U0000ff21-\U0000ff3a"
    r"\U0000ff41-\U0000ff5a"
    r"\U00002160-\U0000217f]"
)

# Scripts that must be rendered with a non-Latin alphabet when properly
# translated. Used as a sanity check that a translation is not a plain
# English pass-through.
_NON_LATIN_LANGS = {"ur", "ar", "hi", "bn", "ru", "zh_cn", "zh_tw", "ja", "ko", "my"}

# Tokens that are preserved verbatim through translation and must NOT count as
# "Latin words" when judging whether a translation actually happened:
# ``{phone}`` placeholders, leftover ``@@T0@@`` masks, ``/commands``, URLs and
# @handles. Without stripping these, a short placeholder-heavy template could
# be rejected as a failed translation even when it was translated correctly.
_RATIO_STRIP_RE = re.compile(
    r"\{[a-zA-Z0-9_]+\}"
    r"|@@T\d+@@"
    r"|/[a-zA-Z0-9_]+"
    r"|https?://[^\s]+"
    r"|@[a-zA-Z0-9_]+"
)


def _looks_like_failed_translation(source_text: str, translated: str, lang: str) -> bool:
    """Heuristic guard: never store obviously broken auto-translations.

    Returns ``True`` when the result is empty, still carries styled/decorated
    characters, is byte-identical to the source after normalization, or is a
    Latin pass-through for a non-Latin target script.
    """
    if not translated or not translated.strip():
        return True
    if _STYLED_CHARS_RE.search(translated):
        return True
    norm_source = unicodedata.normalize("NFKC", source_text).strip()
    norm_translated = unicodedata.normalize("NFKC", translated).strip()
    if norm_translated == norm_source:
        return True
    if lang in _NON_LATIN_LANGS:
        # Judge only the actual translated words: placeholders, commands, URLs
        # and handles are preserved tokens, not evidence of a failed
        # translation.
        plain = _RATIO_STRIP_RE.sub("", norm_translated)
        letters = [c for c in plain if c.isalpha()]
        if letters:
            ascii_letters = sum(1 for c in letters if ("a" <= c <= "z") or ("A" <= c <= "Z"))
            if ascii_letters / len(letters) > 0.5:
                return True
    return False


def to_google_lang(lang: str) -> str:
    """Map one of our 13 language codes to a Google Translate language code."""
    return GOOGLE_LANG_MAP.get(lang, lang or DEFAULT_LANGUAGE)


def _source_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _mask(text: str) -> tuple:
    """Replace placeholders / URLs / @handles with ``@@T<n>@@`` tokens.

    Returns ``(masked, tokens)`` where ``tokens`` is a ``token -> original``
    mapping used to restore the original text after translation.
    """
    tokens = {}

    def _repl(m):
        token = f"@@T{len(tokens)}@@"
        tokens[token] = m.group(0)
        return token

    masked = _MASK_RE.sub(_repl, text)
    return masked, tokens


def _restore(masked: str, tokens: dict) -> str:
    """Restore ``@@T<n>@@`` mask tokens (tolerating provider mangling).

    MyMemory occasionally re-writes the mask (extra spaces, dropped ``@``), so
    the matching is lenient: any ``@...T<n>...@`` arrangement is resolved back
    to the original placeholder/URL/handle.
    """
    def _repl(m):
        return tokens.get(f"@@T{m.group(1)}@@", m.group(0))
    return _TOKEN_RESTORE_RE.sub(_repl, masked)


def _chunk_text(text: str) -> list:
    """Split text into chunks that fit in a single translate request."""
    if len(text) <= _MAX_CHUNK:
        return [text]
    chunks = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current and current_len + line_len > _MAX_CHUNK:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _translate_google_chunk(chunk: str, sl: str, tl: str, timeout: float) -> str:
    """Translate one chunk via the Google free endpoint.

    Tries every endpoint in order, with one retry per endpoint. When the
    endpoint rate-limits us (HTTP 429) a circuit breaker opens so the rest of
    the batch skips Google entirely and uses the fallback instead. Returns the
    translated chunk, or ``None`` if every endpoint failed.
    """
    if _google_breaker_open():
        return None

    last_err = None
    saw_429 = False
    for endpoint, client in _GOOGLE_ENDPOINTS:
        params = {
            "client": client,
            "sl": sl,
            "tl": tl,
            "dt": "t",
            "q": chunk,
        }
        url = endpoint + "?" + urllib.parse.urlencode(params)
        for attempt in range(_MAX_RETRIES + 1):
            _throttle_google()
            try:
                resp = requests.get(url, timeout=timeout)
                if resp.status_code == 429:
                    saw_429 = True
                    last_err = "HTTP 429 (rate limited)"
                    time.sleep(2.0)
                    continue
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}"
                    continue
                data = resp.json()
                parts = data[0] if data else []
                translated = "".join(seg[0] for seg in parts if isinstance(seg, list) and seg)
                if translated:
                    return translated
                last_err = "empty response"
            except Exception as e:  # noqa: BLE001 - network errors are expected
                last_err = repr(e)
    if saw_429:
        _open_google_breaker()
        logger.debug("Google rate-limited, circuit breaker opened for %.0fs", _GOOGLE_BREAKER_COOLDOWN)
    if last_err:
        logger.debug("Google translate unavailable (%s) - trying fallback", last_err)
    return None


def _translate_mymemory_chunk(chunk: str, sl: str, tl: str, timeout: float) -> str:
    """Translate one chunk via MyMemory's free public API (no key required).

    Used as a fallback when Google rate-limits us, so a template still gets
    translated into every language even while Google is throttled. ``sl``/``tl``
    are Google-style codes, which MyMemory accepts as-is (``zh-CN`` etc.).
    Returns the translated chunk, or ``None`` on failure.
    """
    try:
        resp = requests.get(
            _MYMEMORY_URL,
            params={"q": chunk, "langpair": f"{sl}|{tl}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        try:
            status = int(data.get("responseStatus", 0))
        except (TypeError, ValueError):
            status = 0
        if status != 200:
            return None
        translated = data.get("responseData", {}).get("translatedText")
        return translated or None
    except Exception:  # noqa: BLE001 - network errors are expected
        return None


def translate_text(text: str, target_lang: str, source_lang: str = DEFAULT_LANGUAGE, timeout: float = None):
    """Translate ``text`` into ``target_lang`` (our language code).

    ``source_lang`` is the language ``text`` is written in (defaults to
    English) and is passed as the request's ``sl`` so Google Translate knows
    what it is translating *from*. When the admin uploads a template in e.g.
    Bengali, ``source_lang="bn"`` makes every other language derive from the
    Bengali source correctly.

    ``timeout`` overrides the default per-request timeout (used by the
    admin-panel lazy translation so a slow network never stalls a panel
    render for long).

    Requests to the free Google endpoint are throttled globally and retried
    with backoff; when Google is rate-limited (HTTP 429) the free MyMemory API
    is used as a fallback, so translations still complete.

    Returns the translated string, or ``None`` on failure. Blocking — call
    through ``asyncio.to_thread`` from async code.
    """
    if not text or not text.strip():
        return text
    if target_lang == source_lang:
        return text
    if timeout is None:
        timeout = _REQUEST_TIMEOUT

    # Normalize decorated/styled Unicode (mathematical alphanumerics, fullwidth
    # forms) to plain ASCII so Google Translate actually translates the words
    # instead of passing the styled source through verbatim.
    text = unicodedata.normalize("NFKC", text)

    tl = to_google_lang(target_lang)
    sl = to_google_lang(source_lang or DEFAULT_LANGUAGE)
    masked, tokens = _mask(text)
    out_parts = []
    for chunk in _chunk_text(masked):
        translated = _translate_google_chunk(chunk, sl, tl, timeout)
        if not translated:
            translated = _translate_mymemory_chunk(chunk, sl, tl, timeout)
        if not translated:
            logger.warning("translate_text failed for %s", tl)
            return None
        out_parts.append(translated)
    return _restore("".join(out_parts), tokens)


def _template_meta(template_key: str) -> dict:
    meta = _storage.get("message_templates_meta") or {}
    entry = meta.get(template_key) or {"source": DEFAULT_LANGUAGE, "manual": [], "auto": {}}
    entry.setdefault("source", DEFAULT_LANGUAGE)
    entry.setdefault("manual", [])
    entry.setdefault("auto", {})
    return entry


def _save_template_meta(template_key: str, entry: dict) -> None:
    meta = _storage.get("message_templates_meta") or {}
    meta[template_key] = entry
    _storage["message_templates_meta"] = meta
    from bot.data.state import _mark_settings_key_dirty
    _mark_settings_key_dirty("message_templates_meta")
    save_storage()


def mark_template_lang_manual(template_key: str, lang: str, manual: bool = True) -> None:
    """Mark a language as manually edited by the admin.

    Manually edited languages are never overwritten by automatic translation.
    This includes English — if the admin uploads an English template and later
    uploads e.g. a Bengali one, the English text the admin wrote is preserved.
    """
    entry = _template_meta(template_key)
    if manual:
        if lang not in entry["manual"]:
            entry["manual"].append(lang)
        entry["auto"].pop(lang, None)
    else:
        if lang in entry["manual"]:
            entry["manual"].remove(lang)
    _save_template_meta(template_key, entry)


def auto_translate_template(template_key: str, source_text: str, source_lang: str = DEFAULT_LANGUAGE) -> int:
    """Translate ``source_text`` into every other supported language and store it.

    ``source_lang`` is the language ``source_text`` is written in (defaults to
    English). The source language itself is stored verbatim by the caller and is
    never overwritten here. Idempotent: languages that already carry a
    translation of this exact source (same MD5) and manual languages are
    skipped. Only languages that actually produced a translation are stored, so
    a single network failure never corrupts the template. Blocking — call from
    async code via ``asyncio.to_thread``. Returns the number of languages newly
    translated.
    """
    if not source_text or not str(source_text).strip():
        return 0
    source_text = str(source_text)
    if source_lang not in LANGUAGES:
        source_lang = DEFAULT_LANGUAGE
    entry = _template_meta(template_key)
    source_hash = _source_hash(source_text)
    updated = 0
    for lang in LANGUAGES:
        if lang == source_lang:
            continue
        if lang in entry["manual"]:
            continue
        if entry["auto"].get(lang) == source_hash:
            # Skip only if the stored translation is clean; a stored value that
            # still carries mangled mask tokens (e.g. ``@T0@@``) is re-translated.
            existing_map = _storage.get("message_templates") or {}
            existing = existing_map.get(template_key) or {}
            existing_text = existing.get(lang) if isinstance(existing, dict) else None
            if isinstance(existing_text, str) and _MANGLED_TOKEN_RE.search(existing_text):
                entry["auto"].pop(lang, None)
            else:
                continue
        translated = translate_text(source_text, lang, source_lang=source_lang)
        if not translated:
            continue
        if _looks_like_failed_translation(source_text, translated, lang):
            logger.warning("auto_translate_template: skipping garbage translation for %s/%s", template_key, lang)
            continue
        try:
            set_message_template(template_key, translated, lang=lang)
        except Exception as e:  # noqa: BLE001
            logger.warning("auto_translate_template: store %s/%s failed: %s", template_key, lang, e)
            continue
        entry["auto"][lang] = source_hash
        updated += 1
    if updated or entry.get("source") != source_lang:
        entry["source"] = source_lang
        _save_template_meta(template_key, entry)
    return updated


async def auto_translate_template_async(template_key: str, source_text: str, source_lang: str = DEFAULT_LANGUAGE) -> int:
    """Async wrapper of :func:`auto_translate_template` (runs in a thread)."""
    import asyncio
    return await asyncio.to_thread(auto_translate_template, template_key, source_text, source_lang)


def backfill_existing_templates() -> dict:
    """Translate every existing admin-custom template into all languages.

    The source language is taken from ``message_templates_meta`` (recorded when
    the admin last uploaded the template); templates uploaded before the
    source-language tracking existed default to English. Called at startup / via
    admin so templates uploaded before this feature was added also become
    available in every language. Returns a report dict.
    """
    import asyncio
    report = {"processed": 0, "translated": 0, "failed": [], "skipped": []}
    for key, entry in (_storage.get("message_templates") or {}).items():
        if not isinstance(entry, dict):
            entry = {DEFAULT_LANGUAGE: entry}
        meta = _template_meta(key)
        source_lang = meta.get("source") or DEFAULT_LANGUAGE
        source = entry.get(source_lang)
        if not source:
            # The recorded source no longer exists (e.g. template uploaded in a
            # language that was later reset); fall back to English as source.
            source_lang = DEFAULT_LANGUAGE
            source = entry.get(source_lang)
        if not source:
            continue
        report["processed"] += 1
        try:
            done = asyncio.run(auto_translate_template_async(key, source, source_lang=source_lang))
            report["translated"] += done
            if done == 0:
                report["skipped"].append(key)
        except Exception as e:  # noqa: BLE001
            logger.error("backfill failed for %s: %s", key, e)
            report["failed"].append(key)
    return report
