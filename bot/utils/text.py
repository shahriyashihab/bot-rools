"""Safe text helpers for Pyrogram/Telegram messages.

Pyrogram's ``Message.text`` is a ``Str`` subclass (a ``str`` with extra
entity helpers) whose internal storage keeps emoji and other supplementary
plane characters as UTF-16 surrogate pairs.  Slicing that object directly
goes through ``Str.__getitem__`` which re-encodes the string; if the slice
cuts a surrogate pair in half the decode step raises a
``UnicodeDecodeError: unexpected end of data``.  Even a plain ``str`` that
still contains surrogate pairs crashes later when it is encoded to UTF-8
(e.g. when sending a message or writing to a log).

These helpers collapse surrogate pairs back into real code points first, so
any subsequent slicing, length checks and encoding are always safe.
"""


def safe_text(value) -> str:
    """Return a plain ``str`` with surrogate pairs collapsed into real code points.

    Lone surrogates (malformed input, not part of a pair) are stripped
    instead of being allowed to crash a later encode step.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return text
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeDecodeError:
        return "".join(c for c in text if not (0xD800 <= ord(c) <= 0xDFFF))


def truncate(value, limit: int, ellipsis: str = "...") -> str:
    """Truncate text to at most ``limit`` chars, appending ``ellipsis`` if cut.

    Input may be a ``pyrogram.types.Message.text`` style ``Str``; surrogate
    pairs are normalized before truncation so no pair is ever split.
    """
    text = safe_text(value)
    if len(text) <= limit:
        return text
    budget = max(0, limit - len(ellipsis))
    return text[:budget] + ellipsis
