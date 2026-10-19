"""
Supported Languages Catalog
===========================
Central definition of the 13 supported languages with their native names,
English names and country flags. Used by the language-selection UI, the
translation engine and the admin panel language setting.

Order here defines the order shown in the language-selection keyboard.
"""

LANGUAGES = {
    "en": {
        "name": "English",
        "native": "English",
        "flag": "\U0001f1ec\U0001f1e7",
        "rtl": False,
    },
    "ur": {
        "name": "Urdu",
        "native": "\u0627\u0631\u062f\u0648",
        "flag": "\U0001f1f5\U0001f1f0",
        "rtl": True,
    },
    "ar": {
        "name": "Arabic",
        "native": "\u0627\u0644\u0639\u0631\u0628\u064a\u0629",
        "flag": "\U0001f1f8\U0001f1e6",
        "rtl": True,
    },
    "hi": {
        "name": "Hindi",
        "native": "\u0939\u093f\u0928\u094d\u0926\u0940",
        "flag": "\U0001f1ee\U0001f1f3",
        "rtl": False,
    },
    "bn": {
        "name": "Bengali",
        "native": "\u09ac\u09be\u0982\u09b2\u09be",
        "flag": "\U0001f1e7\U0001f1e9",
        "rtl": False,
    },
    "id": {
        "name": "Indonesian",
        "native": "Bahasa Indonesia",
        "flag": "\U0001f1ee\U0001f1e9",
        "rtl": False,
    },
    "es": {
        "name": "Spanish",
        "native": "Espa\u00f1ol",
        "flag": "\U0001f1ea\U0001f1f8",
        "rtl": False,
    },
    "ru": {
        "name": "Russian",
        "native": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439",
        "flag": "\U0001f1f7\U0001f1fa",
        "rtl": False,
    },
    "zh_cn": {
        "name": "Simplified Chinese",
        "native": "\u7b80\u4f53\u4e2d\u6587",
        "flag": "\U0001f1e8\U0001f1f3",
        "rtl": False,
    },
    "zh_tw": {
        "name": "Traditional Chinese",
        "native": "\u7e41\u9ad4\u4e2d\u6587",
        "flag": "\U0001f1f9\U0001f1fc",
        "rtl": False,
    },
    "ja": {
        "name": "Japanese",
        "native": "\u65e5\u672c\u8a9e",
        "flag": "\U0001f1ef\U0001f1f5",
        "rtl": False,
    },
    "ko": {
        "name": "Korean",
        "native": "\ud55c\uad6d\uc5b4",
        "flag": "\U0001f1f0\U0001f1f7",
        "rtl": False,
    },
    "my": {
        "name": "Burmese",
        "native": "\u1019\u103c\u1014\u103a\u1019\u102c",
        "flag": "\U0001f1f2\U0001f1f2",
        "rtl": False,
    },
}

DEFAULT_LANGUAGE = "en"

# All valid language codes (kept as a set for fast validation)
LANGUAGE_CODES = frozenset(LANGUAGES.keys())

LANGUAGE_SELECT_CALLBACK_PREFIX = "set_lang_"


def is_supported_language(lang: str) -> bool:
    """Return True if ``lang`` is one of the supported language codes."""
    return lang in LANGUAGE_CODES


def get_language_flag(lang: str) -> str:
    """Return the flag emoji for a language code (falls back to English flag)."""
    lang = lang or DEFAULT_LANGUAGE
    info = LANGUAGES.get(lang)
    if info:
        return info["flag"]
    return LANGUAGES[DEFAULT_LANGUAGE]["flag"]


def get_language_native_name(lang: str) -> str:
    """Return the native name of a language (falls back to English)."""
    lang = lang or DEFAULT_LANGUAGE
    info = LANGUAGES.get(lang)
    if info:
        return info["native"]
    return LANGUAGES[DEFAULT_LANGUAGE]["native"]


def get_language_english_name(lang: str) -> str:
    """Return the English name of a language (falls back to English)."""
    lang = lang or DEFAULT_LANGUAGE
    info = LANGUAGES.get(lang)
    if info:
        return info["name"]
    return LANGUAGES[DEFAULT_LANGUAGE]["name"]


def is_rtl_language(lang: str) -> bool:
    """Return True if the language is right-to-left (Urdu, Arabic)."""
    info = LANGUAGES.get(lang or DEFAULT_LANGUAGE)
    return bool(info and info.get("rtl"))


def build_language_label(lang: str) -> str:
    """Build a single language label: `<flag> <native>` used in keyboards."""
    info = LANGUAGES.get(lang)
    if not info:
        info = LANGUAGES[DEFAULT_LANGUAGE]
    return f"{info['flag']} {info['native']}"


def build_language_grid_rows(last_lang: str = DEFAULT_LANGUAGE) -> list:
    """Arrange the 13 languages as a 2-column keyboard grid.

    Layout:
        6 rows x 2 columns  = 12 languages (side-by-side, equal boxes)
        1 final full-width row = ``last_lang`` (default: English)

    Returns a list of rows; each row is a list of language codes. Callers
    build their own buttons from the codes with their own callback prefix.
    """
    if last_lang not in LANGUAGES:
        last_lang = DEFAULT_LANGUAGE
    grid_codes = [code for code in LANGUAGES if code != last_lang]
    rows = []
    for i in range(0, len(grid_codes), 2):
        rows.append(grid_codes[i:i + 2])
    rows.append([last_lang])
    return rows
