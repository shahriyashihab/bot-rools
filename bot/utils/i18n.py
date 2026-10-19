"""
Internationalization (i18n) helpers
===================================
Provides user-language lookup/storage and the translation function ``t()``
used by all handlers. Admin panel has its own language preference stored
separately (default English).
"""

import re
import threading
import logging

from bot.data.languages import (
    LANGUAGES,
    DEFAULT_LANGUAGE,
    LANGUAGE_CODES,
    is_supported_language,
)
from bot.data.translations import TRANSLATIONS

logger = logging.getLogger(__name__)

_ADMIN_LANGS = {}
_ADMIN_LANGS_LOCK = threading.RLock()


def get_user_lang(user_id) -> str:
    """Return the stored language code for a user (default English)."""
    try:
        from bot.utils.fast_cache import get_cached_user
        cached = get_cached_user(int(user_id))
        if cached and is_supported_language(cached.get("lang", "")):
            return cached["lang"]
    except (ImportError, TypeError, ValueError):
        pass
    try:
        from bot.utils.storage import get_user_info
        user = get_user_info(user_id, create_if_missing=False)
        lang = user.get("lang", "")
        if is_supported_language(lang):
            return lang
    except Exception:
        pass
    return DEFAULT_LANGUAGE


def has_explicit_lang(user_id) -> bool:
    """True if the user has explicitly chosen a supported language."""
    try:
        from bot.utils.storage import get_user_info
        user = get_user_info(user_id, create_if_missing=False)
        return bool(user and is_supported_language(user.get("lang", "")))
    except Exception:
        return False


def set_user_lang(user_id, lang: str) -> None:
    """Store the selected language for a user and refresh caches.

    The admin-panel language is kept in sync, so a language chosen in the
    normal picker also applies to the admin panel (everything follows the
    same language the admin picked).
    """
    if not is_supported_language(lang):
        lang = DEFAULT_LANGUAGE
    try:
        from bot.utils.storage import update_user_info
        update_user_info(user_id, lang=lang, admin_lang=lang)
    except Exception as e:
        logger.warning(f"Could not save user lang {user_id}: {e}")
    with _ADMIN_LANGS_LOCK:
        _ADMIN_LANGS.pop(int(user_id), None)
    try:
        from bot.utils.fast_cache import sync_user_from_storage, invalidate_user_cache
        invalidate_user_cache(int(user_id))
        sync_user_from_storage(int(user_id))
    except (ImportError, ValueError, TypeError):
        pass


def get_admin_lang(user_id) -> str:
    """Return the admin-panel language for an admin.

    Resolution order:
      1. In-memory override set via ``set_admin_lang`` (admin panel picker)
      2. ``admin_lang`` persisted in the user record
      3. The admin's selected user-facing language (so the panel follows the
         same language the admin chose in the normal language picker)
      4. English default
    """
    with _ADMIN_LANGS_LOCK:
        lang = _ADMIN_LANGS.get(int(user_id), "")
        if is_supported_language(lang):
            return lang
    try:
        from bot.utils.storage import get_user_info
        user = get_user_info(user_id, create_if_missing=False)
        lang = user.get("admin_lang", "")
        if is_supported_language(lang):
            with _ADMIN_LANGS_LOCK:
                _ADMIN_LANGS[int(user_id)] = lang
            return lang
    except Exception:
        pass
    return get_user_lang(user_id)


def set_admin_lang(user_id, lang: str) -> None:
    """Store the admin-panel language preference for an admin.

    Passing an empty or unsupported ``lang`` clears any in-memory override so
    ``get_admin_lang`` falls back to the user's selected language.

    The user-facing language is kept in sync too, so a panel-language choice
    also applies to the admin's user-facing messages (``/start`` welcome, etc.).
    """
    with _ADMIN_LANGS_LOCK:
        if lang and is_supported_language(lang):
            _ADMIN_LANGS[int(user_id)] = lang
        else:
            _ADMIN_LANGS.pop(int(user_id), None)
    try:
        from bot.utils.storage import update_user_info
        if lang and is_supported_language(lang):
            update_user_info(user_id, admin_lang=lang, lang=lang)
        else:
            update_user_info(user_id, admin_lang="")
    except Exception as e:
        logger.warning(f"Could not save admin lang {user_id}: {e}")
    try:
        from bot.utils.fast_cache import sync_user_from_storage, invalidate_user_cache
        invalidate_user_cache(int(user_id))
        sync_user_from_storage(int(user_id))
    except (ImportError, ValueError, TypeError):
        pass


def _lookup(key: str, lang: str) -> str:
    """Return the translated text for a key/lang with English fallback."""
    by_lang = TRANSLATIONS.get(key)
    if by_lang:
        text = by_lang.get(lang)
        if text is not None:
            return text
        text = by_lang.get(DEFAULT_LANGUAGE)
        if text is not None:
            return text
    return None


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Translate ``key`` into ``lang`` and substitute ``{placeholder}`` values.

    Falls back to the English translation, then to the raw key.
    """
    if not lang or not is_supported_language(lang):
        lang = DEFAULT_LANGUAGE

    text = _lookup(key, lang)

    if text is None:
        try:
            from bot.utils.storage import DEFAULT_MESSAGE_TEMPLATES
            text = DEFAULT_MESSAGE_TEMPLATES.get(key)
        except Exception:
            text = None

    if text is None:
        text = f"[{key}]"

    try:
        for name, value in kwargs.items():
            text = re.sub(r'\{' + re.escape(name) + r'\}', lambda m: str(value), text)
    except Exception:
        pass
    return text


def user_t(key: str, user_id, **kwargs) -> str:
    """Translate ``key`` into the stored language of ``user_id``."""
    return t(key, get_user_lang(user_id), **kwargs)


def admin_t(key: str, admin_id, **kwargs) -> str:
    """Translate ``key`` into the stored admin-panel language of ``admin_id``."""
    return t(key, get_admin_lang(admin_id), **kwargs)


_DYNAMIC_RE = None

_LAZY_TRANSLATE_CACHE = {}
_LAZY_TRANSLATE_LOCK = threading.RLock()
_LAZY_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_LAZY_JSON_RE = re.compile(r'"[a-zA-Z0-9_]+"\s*:')
_NON_LATIN_LANGS = {"ur", "ar", "hi", "bn", "ru", "zh_cn", "zh_tw", "ja", "ko", "my"}


def _lazy_translation_ok(source: str, translated: str, lang: str) -> bool:
    """Lighter sanity guard than the template one, suitable for admin lines.

    Admin lines legitimately keep Latin tokens (API ids, currency codes, …) so
    the strict Latin-dominance check would reject valid translations. Here we
    only reject empty/no-op results and, for non-Latin scripts, results that
    carry no non-Latin alphabet characters at all (i.e. untouched English).
    """
    if not translated or not translated.strip():
        return False
    if translated.strip() == source.strip():
        return False
    if lang in _NON_LATIN_LANGS:
        has_script = any((not c.isascii()) and c.isalpha() for c in translated)
        if not has_script:
            return False
    return True


def _dynamic_template_match(line: str):
    """Match a runtime-formatted line against ``{x}`` template translation keys.

    Returns ``(key, translated_by_lang, groups)`` for the first template whose
    pattern (with ``{x}`` as ``(.*?)``) matches the whole line, or ``None``.
    Pure-placeholder templates (e.g. ``{x}``) are skipped to avoid false matches.
    """
    global _DYNAMIC_RE
    if _DYNAMIC_RE is None:
        try:
            from bot.data.admin_translations import ADMIN_TRANSLATIONS
        except Exception:
            _DYNAMIC_RE = []
            return None
        compiled = []
        for key, bylang in ADMIN_TRANSLATIONS.items():
            if "{x}" not in key:
                continue
            if not key.replace("{x}", "").strip():
                continue
            parts = key.split("{x}")
            pattern = ""
            for i, part in enumerate(parts):
                pattern += re.escape(part)
                if i < len(parts) - 1:
                    pattern += "(.*?)"
            static_len = sum(len(p) for p in parts)
            compiled.append((static_len, re.compile("^" + pattern + "$"), key, bylang))
        # Most specific (longest static text) templates first, so generic
        # leading-{x} patterns do not shadow exact country/menu templates.
        compiled.sort(key=lambda c: c[0], reverse=True)
        _DYNAMIC_RE = compiled
    for static_len, rx, key, bylang in _DYNAMIC_RE:
        m = rx.match(line)
        if m:
            return key, bylang, m.groups()
    return None


def _lazy_translatable(line: str) -> bool:
    """True when a line is likely English UI text worth lazy-translating.

    Pure data (user ids, JSON, code fences, numbers-only) is left untouched so
    nothing is ever mangled.
    """
    if not line or not isinstance(line, str):
        return False
    s = line.strip()
    if not s:
        return False
    if len(s) < 3 or len(s) > 700:
        return False
    if "\n" in s:
        return False
    if s.startswith(("{", "[", "`")) or s.startswith("```"):
        return False
    if _LAZY_JSON_RE.search(s):
        return False
    if not _LAZY_WORD_RE.search(s):
        return False
    letters = sum(1 for c in s if c.isalpha())
    if letters / max(len(s), 1) < 0.2:
        return False
    return True


def _lazy_translate_batch(lines, lang: str):
    """Translate a batch of leftover English lines via the translator service.

    All lines are translated in ONE request (joined with newlines) so screens
    with many data rows cost a single round-trip, then cached line-by-line in
    memory. Returns a dict ``{original_line: translated}`` for the lines that
    were actually translated. Never raises.
    """
    if not lines or lang == DEFAULT_LANGUAGE:
        return {}
    from bot.services.translator import translate_text

    with _LAZY_TRANSLATE_LOCK:
        pending = [ln for ln in lines if (ln, lang) not in _LAZY_TRANSLATE_CACHE]
        cached_map = {ln: _LAZY_TRANSLATE_CACHE[(ln, lang)] for ln in lines if (ln, lang) in _LAZY_TRANSLATE_CACHE}
    if not pending:
        return cached_map

    joined = "\n".join(pending)
    try:
        translated = translate_text(joined, lang, timeout=5)
    except Exception:
        return cached_map
    if not translated or translated == joined:
        return cached_map
    translated_lines = translated.split("\n")

    result = dict(cached_map)
    if len(translated_lines) == len(pending):
        for orig, new in zip(pending, translated_lines):
            if not new or new.strip() == orig.strip():
                continue
            if not _lazy_translation_ok(orig, new, lang):
                continue
            result[orig] = new
            with _LAZY_TRANSLATE_LOCK:
                _LAZY_TRANSLATE_CACHE[(orig, lang)] = new
                try:
                    from bot.data.admin_translations import ADMIN_TRANSLATIONS
                    by_lang = ADMIN_TRANSLATIONS.setdefault(orig, {})
                    by_lang.setdefault(lang, new)
                except Exception:
                    pass
    else:
        # Line count changed after translation: translate the stragglers one
        # by one so we never mangle the structure.
        for orig in pending:
            try:
                one = translate_text(orig, lang, timeout=5)
                if one and _lazy_translation_ok(orig, one, lang):
                    result[orig] = one
                    with _LAZY_TRANSLATE_LOCK:
                        _LAZY_TRANSLATE_CACHE[(orig, lang)] = one
                        try:
                            from bot.data.admin_translations import ADMIN_TRANSLATIONS
                            by_lang = ADMIN_TRANSLATIONS.setdefault(orig, {})
                            by_lang.setdefault(lang, one)
                        except Exception:
                            pass
            except Exception:
                pass
    return result


def admin_str(admin_id, text: str) -> str:
    """Translate a static English admin-panel string into the admin's language.

    The admin panel is large and mostly hardcoded English, so translations are
    looked up by the exact English source string. Strings without a matching
    translation (e.g. data rows) are returned unchanged.

    Multi-line messages are handled by translating every individual line that
    has a matching translation key and re-joining them, so long admin menu
    headers render fully translated even when the whole block is not a single
    key. Formatted (dynamic) lines are matched against ``{x}`` template keys so
    values are re-spliced into the translated template. Untranslated lines
    (dynamic data rows, etc.) stay as-is.

    Any single line that STILL has no pre-translated key but looks like English
    UI text is translated lazily through the automatic translator service and
    cached, so a non-English admin never sees a mixed-language panel.
    """
    if not text or not isinstance(text, str):
        return text
    lang = get_admin_lang(admin_id)
    if lang == DEFAULT_LANGUAGE:
        return text
    try:
        from bot.data.admin_translations import ADMIN_TRANSLATIONS
    except Exception:
        return text

    def _translate_one(s):
        by_lang = ADMIN_TRANSLATIONS.get(s) or ADMIN_TRANSLATIONS.get(s.strip())
        if by_lang:
            translated = by_lang.get(lang)
            if not translated:
                translated = by_lang.get(DEFAULT_LANGUAGE)
            if translated:
                return translated, False
            return None
        dm = _dynamic_template_match(s.strip())
        if dm:
            key, by_lang, groups = dm
            translated = by_lang.get(lang) or by_lang.get(DEFAULT_LANGUAGE)
            if translated:
                for g in groups:
                    translated = translated.replace("{x}", g, 1)
                return translated, True
        return None

    result = _translate_one(text)
    if result is not None:
        return result[0]

    # Multi-line fallback: translate each known line independently, then lazy-
    # translate whatever English is still left (batch, one round-trip).
    if "\n" in text:
        lines = text.split("\n")
        out = []
        changed = False
        lazy_needed = []
        for line in lines:
            res = _translate_one(line)
            if res is not None:
                out.append(res[0])
                changed = True
            else:
                out.append(line)
                if _lazy_translatable(line):
                    lazy_needed.append(line)
        if lazy_needed:
            lazy_map = _lazy_translate_batch(lazy_needed, lang)
            for i, line in enumerate(lines):
                if line in lazy_map:
                    out[i] = lazy_map[line]
                    changed = True
        if changed:
            return "\n".join(out)

    # Single line that has no pre-translated key: lazy translate it directly.
    if _lazy_translatable(text):
        lazy_map = _lazy_translate_batch([text], lang)
        if text in lazy_map:
            return lazy_map[text]

    return text


def admin_translate_markup(admin_id, markup):
    """Return an InlineKeyboardMarkup whose button labels are translated.

    Falls back to the original markup if translation is not possible.
    """
    if markup is None:
        return None
    if get_admin_lang(admin_id) == DEFAULT_LANGUAGE:
        return markup
    try:
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        if not isinstance(markup, InlineKeyboardMarkup):
            return markup
        rows = []
        for row in markup.inline_keyboard:
            new_row = []
            for btn in row:
                kwargs = {}
                if getattr(btn, "url", None):
                    kwargs["url"] = btn.url
                if getattr(btn, "callback_data", None):
                    kwargs["callback_data"] = btn.callback_data
                if getattr(btn, "switch_inline_query", None):
                    kwargs["switch_inline_query"] = btn.switch_inline_query
                if getattr(btn, "switch_inline_query_current_chat", None):
                    kwargs["switch_inline_query_current_chat"] = btn.switch_inline_query_current_chat
                if getattr(btn, "callback_game", None):
                    kwargs["callback_game"] = btn.callback_game
                if getattr(btn, "pay", None):
                    kwargs["pay"] = btn.pay
                if getattr(btn, "login_url", None):
                    kwargs["login_url"] = btn.login_url
                if getattr(btn, "user_id", None):
                    kwargs["user_id"] = btn.user_id
                new_row.append(
                    InlineKeyboardButton(admin_str(admin_id, btn.text), **kwargs)
                )
            rows.append(new_row)
        return InlineKeyboardMarkup(rows)
    except Exception:
        return markup


def language_flag_name(lang: str) -> str:
    """Return ``<flag> <native name>`` for a language code."""
    from bot.data.languages import build_language_label
    return build_language_label(lang)
