"""Refactored from bot.utils.storage: Country / rate storage operations."""
import os
import re
from typing import Optional, Tuple

from bot.data.state import Any, Dict, List, Optional, _mark_dirty, _mark_settings_key_dirty, _storage, _storage_lock, save_storage, save_storage_now, thread_safe
from bot.constants import DEFAULT_COUNTRY_CAPACITY, DEFAULT_TIMEOUT_SECONDS

# ------------------------------------------------------------------
# Per-country feature switches / extra config (CORE CONFIG feature).
#
# Every field lives INSIDE the country record itself, so a write for one
# country can never touch another. All fields are OPTIONAL: an old record
# that predates this feature simply has none of them and every getter below
# falls back to the existing GLOBAL setting (or a safe default), so old data
# loads unchanged and no migration is required.
#   accept_restricted: True/False (fallback: True)
#   accept_spam:       True/False (fallback: global spam-check enabled state)
#   twofa_check:       True/False (fallback: global two_factor_enabled)
#   device_check:      True/False (fallback: global device_check_enabled)
#   accept_gmail:      True/False (fallback: True)
#   platform:          "android" / "ios" (fallback: existing per-country
#                      device heuristics, i.e. no override)
#   custom_api:        {"api_id": int, "api_hash": str} or absent
#   name_enabled / bio_enabled: True/False (fallback: True)
#   name_list / bio_list: list[str] or absent
# ------------------------------------------------------------------

_CC_BOOL_DEFAULTS = {
    "accept_restricted": True,
    "accept_spam": True,
    "twofa_check": True,
    "device_check": True,
    "accept_gmail": True,
    "name_enabled": True,
    "bio_enabled": True,
    "photo_enabled": False,
}

_CC_PLATFORM_VALUES = ("android", "ios")


def _country_data_or_none(country_code: str) -> Optional[Dict[str, Any]]:
    """Raw country record for the EXACT code (no fallback chain lookups)."""
    data = _storage.get("countries", {}).get(country_code)
    return data if isinstance(data, dict) else None


def _get_country_flag_value(country_code: str, key: str) -> Optional[bool]:
    """Stored per-country boolean or None when unset (caller applies fallback)."""
    data = _country_data_or_none(country_code)
    if data is None or key not in data:
        return None
    return bool(data.get(key))


def _set_country_flag_value(country_code: str, key: str, value: bool) -> bool:
    """Persist one per-country boolean. Returns False when no such country."""
    if country_code not in _storage.get("countries", {}):
        return False
    _storage["countries"][country_code][key] = bool(value)
    _mark_dirty("countries", country_code)
    save_storage()
    return True


def _global_setting_flag(fn_name: str, default: bool = True) -> bool:
    """Read a global feature switch through the SAME import path the old
    call sites used (bot.utils.storage re-exports). This keeps behaviour
    identical for every consumer that patched/monkeypatched the global
    switch there."""
    try:
        import bot.utils.storage as _shim
        fn = getattr(_shim, fn_name, None)
        if callable(fn):
            return bool(fn())
    except Exception:
        pass
    return default


def get_country_setting_bool(country_code: str, key: str) -> bool:
    """Per-country boolean with the documented global/default fallback."""
    stored = _get_country_flag_value(country_code, key)
    if stored is not None:
        return stored
    if key == "accept_spam":
        # Existing global behaviour: the Spam Check master switch decides.
        return _global_setting_flag("is_spam_check_enabled", True)
    if key == "twofa_check":
        return _global_setting_flag("is_two_factor_enabled", True)
    if key == "device_check":
        return _global_setting_flag("is_device_check_enabled", True)
    return _CC_BOOL_DEFAULTS.get(key, True)


def set_country_setting_bool(country_code: str, key: str, value: bool) -> bool:
    """Set one per-country boolean (CORE CONFIG toggle). False = no country."""
    if key not in _CC_BOOL_DEFAULTS:
        return False
    return _set_country_flag_value(country_code, key, bool(value))


def get_country_platform(country_code: str) -> str:
    """Per-country platform override: 'android' / 'ios' / '' (none)."""
    data = _country_data_or_none(country_code)
    if not data:
        return ""
    value = str(data.get("platform", "") or "").strip().lower()
    return value if value in _CC_PLATFORM_VALUES else ""


def set_country_platform(country_code: str, platform: str) -> bool:
    """Set the per-country platform override ('android'/'ios'), or clear with ''."""
    if country_code not in _storage.get("countries", {}):
        return False
    value = str(platform or "").strip().lower()
    if value and value not in _CC_PLATFORM_VALUES:
        return False
    if value:
        _storage["countries"][country_code]["platform"] = value
    else:
        _storage["countries"][country_code].pop("platform", None)
    _mark_dirty("countries", country_code)
    save_storage()
    return True


def get_country_custom_api(country_code: str) -> Optional[Dict[str, Any]]:
    """Per-country custom API {'api_id', 'api_hash'} or None when unset.

    The value is a COPY: mutating the returned dict never touches storage.
    """
    data = _country_data_or_none(country_code)
    if not data:
        return None
    api = data.get("custom_api")
    if not isinstance(api, dict):
        return None
    try:
        return {"api_id": int(api.get("api_id")), "api_hash": str(api.get("api_hash") or "")}
    except (TypeError, ValueError):
        return None


def set_country_custom_api(country_code: str, api_id, api_hash) -> bool:
    """Store a custom API for this country only. False = invalid/no country."""
    if country_code not in _storage.get("countries", {}):
        return False
    try:
        _api_id = int(api_id)
    except (TypeError, ValueError):
        return False
    _api_hash = str(api_hash or "").strip()
    if not _api_hash:
        return False
    _storage["countries"][country_code]["custom_api"] = {
        "api_id": _api_id,
        "api_hash": _api_hash,
    }
    _mark_dirty("countries", country_code)
    save_storage()
    return True


def remove_country_custom_api(country_code: str) -> bool:
    """Remove this country's custom API only. True = something was removed."""
    if country_code not in _storage.get("countries", {}):
        return False
    existed = "custom_api" in _storage["countries"][country_code]
    _storage["countries"][country_code].pop("custom_api", None)
    if existed:
        _mark_dirty("countries", country_code)
        save_storage()
    return existed


def get_country_name_list(country_code: str) -> List[str]:
    """Uploaded name list for this country (copy, [] when unset)."""
    data = _country_data_or_none(country_code)
    value = data.get("name_list") if data else None
    return list(value) if isinstance(value, list) else []


def set_country_name_list(country_code: str, names: List[str]) -> bool:
    """Store the uploaded name list for this country only."""
    if country_code not in _storage.get("countries", {}):
        return False
    _storage["countries"][country_code]["name_list"] = [str(n) for n in names]
    _mark_dirty("countries", country_code)
    save_storage()
    return True


def get_country_bio_list(country_code: str) -> List[str]:
    """Uploaded bio list for this country (copy, [] when unset)."""
    data = _country_data_or_none(country_code)
    value = data.get("bio_list") if data else None
    return list(value) if isinstance(value, list) else []


def set_country_bio_list(country_code: str, bios: List[str]) -> bool:
    """Store the uploaded bio list for this country only."""
    if country_code not in _storage.get("countries", {}):
        return False
    _storage["countries"][country_code]["bio_list"] = [str(b) for b in bios]
    _mark_dirty("countries", country_code)
    save_storage()
    return True


def set_country_capacity(country_code: str, capacity) -> bool:
    """Set the capacity of ONE country (0 = unlimited, existing semantics).

    Uses the existing ``validate_capacity`` rules. When a new explicit
    capacity is set the persistent SUCCESS gate resets, exactly like
    ``set_country_rates`` does - that is the documented way to re-open a
    filled country. Returns False when the country does not exist.
    """
    if country_code not in _storage.get("countries", {}):
        return False
    _storage["countries"][country_code]["capacity"] = validate_capacity(capacity)
    _mark_dirty("countries", country_code)
    try:
        from bot.data.counters import reset_daily_success_count
        reset_daily_success_count(country_code)
    except Exception:
        pass
    save_storage_now()
    return True


# ------------------------------------------------------------------
# CORE CONFIG: per-country PROFILE PHOTOS (ZIP upload feature).
#
# Photo FILES live on disk under one folder per country; the country record
# only carries metadata (count + a rotating cursor). Optional fields with a
# safe fallback: an old record with no photo fields simply means "no photos".
#   photo_enabled:  True/False (fallback: False - off unless uploaded+enabled)
#   photo_count:    int, number of files on disk for this country
#   photo_cursor:   int, next index to hand out (survives restarts)
# ------------------------------------------------------------------

_CC_PHOTO_ALLOWED_EXT = {".jpg", ".jpeg", ".png"}

# P4-46 zip-bomb guard: stream-copy chunk size and the per-import budget for
# DECOMPRESSED bytes. 4 GB of real photos is far beyond any legitimate
# profile set (10 MB/photo -> 400 photos); a highly-compressible 2 GB ZIP
# (the download cap) can no longer expand to disk-death.
_CC_ZIP_CHUNK_SIZE = 1024 * 1024
_CC_ZIP_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


def _photos_root() -> str:
    """Base folder holding every country's photo directories."""
    from bot.data.state import SQLITE_STORAGE_PATH
    base = os.path.dirname(str(SQLITE_STORAGE_PATH) or ".")
    return os.path.join(base or ".", "country_photos")


def get_country_photos_dir(country_code: str, create: bool = True) -> str:
    """Directory that stores ONE country's photo files.

    ``create=True`` makes the folder on demand (import path). Read-only
    callers (counting/listing) pass ``create=False`` so merely VIEWING a
    country's details never creates stray empty folders.
    """
    safe = re.sub(r"[^0-9+]", "", str(country_code or "")) or "unknown"
    path = os.path.join(_photos_root(), safe)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _cc_photo_scan(country_code: str) -> List[str]:
    """Sorted absolute paths of every valid photo file for this country."""
    directory = get_country_photos_dir(country_code, create=False)
    if not os.path.isdir(directory):
        return []
    files = [
        name for name in os.listdir(directory)
        if os.path.splitext(name)[1].lower() in _CC_PHOTO_ALLOWED_EXT
    ]
    files.sort()
    return [os.path.join(directory, name) for name in files]


def get_country_photo_enabled(country_code: str) -> bool:
    """Photo application toggle for this country (default OFF)."""
    stored = _get_country_flag_value(country_code, "photo_enabled")
    return bool(stored) if stored is not None else False


def set_country_photo_zip_meta(country_code: str, meta: Dict[str, Any]) -> None:
    """Remember the last imported ZIP (name/size/time) for the details screen."""
    if country_code not in _storage.get("countries", {}):
        return
    _storage["countries"][country_code]["photo_zip_meta"] = dict(meta or {})
    _mark_dirty("countries", country_code)
    save_storage()


def get_country_photo_zip_meta(country_code: str) -> Optional[Dict[str, Any]]:
    """Last imported ZIP metadata (copy) or None."""
    data = _country_data_or_none(country_code)
    if not data:
        return None
    meta = data.get("photo_zip_meta")
    return dict(meta) if isinstance(meta, dict) else None


def set_country_photo_enabled(country_code: str, value: bool) -> bool:
    """Enable/disable photo application for ONE country. False = no country."""
    if country_code not in _storage.get("countries", {}):
        return False
    _storage["countries"][country_code]["photo_enabled"] = bool(value)
    _mark_dirty("countries", country_code)
    save_storage()
    return True


def get_country_photo_count(country_code: str) -> int:
    """Number of photo files stored on disk for this country (live count)."""
    try:
        directory = get_country_photos_dir(country_code, create=False)
        if not os.path.isdir(directory):
            return 0
        return sum(
            1 for name in os.listdir(directory)
            if os.path.splitext(name)[1].lower() in _CC_PHOTO_ALLOWED_EXT
        )
    except Exception:
        return 0


def get_country_photo_cursor(country_code: str) -> int:
    """Index of the next photo to hand out (persistent rotation cursor)."""
    data = _country_data_or_none(country_code)
    if not data:
        return 0
    try:
        return max(0, int(data.get("photo_cursor", 0) or 0))
    except (TypeError, ValueError):
        return 0


def set_country_photo_cursor(country_code: str, cursor: int) -> None:
    """Persist the rotation cursor so restarts never reuse/repeat photos."""
    if country_code not in _storage.get("countries", {}):
        return
    _storage["countries"][country_code]["photo_cursor"] = max(0, int(cursor))
    _mark_dirty("countries", country_code)
    save_storage()


def get_next_country_photo(country_code: str) -> Optional[str]:
    """Pick the next photo for this country and advance the rotation cursor.

    Deterministic sequential rotation: account N gets photo N, wrapping at
    the end of the list. The cursor is persisted, so bot restarts never
    repeat photos or reset the sequence. Returns None when there are no
    photos (caller skips silently).
    """
    photos = _cc_photo_scan(country_code)
    if not photos:
        return None
    index = get_country_photo_cursor(country_code) % len(photos)
    set_country_photo_cursor(country_code, (index + 1) % len(photos))
    return photos[index]


def import_country_photos_zip(country_code: str, zip_path: str, max_total_bytes: Optional[int] = None) -> Tuple[int, int]:
    """Import photos from a ZIP archive into this country's folder.

    Returns ``(imported, skipped)``. Only .jpg/.jpeg/.png entries are kept;
    everything else (and zero-byte files) is skipped. Entry names are
    SANITIZED: only the basename is used, path separators and traversal
    sequences are rejected, so nothing can ever be written outside this
    country's folder (zip-slip protection). Existing files are overwritten
    by name; the photo cursor is RESET to 0 so the new set starts from the
    beginning.

    P4-46 hardening:
    - STREAMED, SIZE-CAPPED reads: members are copied in chunks through
      ``zf.open()`` (never ``zf.read()``, which loads the whole decompressed
      member into RAM) and any member whose decompressed size would push the
      total past ``max_total_bytes`` (default 4 GB) is skipped - a ZIP bomb
      can no longer exhaust memory or the disk.
    - PER-ENTRY isolation: one bad entry (a Windows-reserved basename like
      ``con.jpg``, illegal filename characters, an OS-level write error, or
      a file locked by an in-flight upload) is SKIPPED - the rest of the
      import continues and the cursor/meta still reset correctly. Previously
      a single such entry aborted the whole import mid-way, leaving a
      partial photo set on disk.
    """
    import zipfile
    if max_total_bytes is None:
        max_total_bytes = _CC_ZIP_MAX_TOTAL_BYTES
    imported = skipped = 0
    directory = get_country_photos_dir(country_code)
    dir_abs = os.path.abspath(directory)
    total_written = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            # SANITIZE (zip-slip): sub-folder photos are FLATTENED to their
            # basename, but any entry containing traversal ("..") or a drive
            # letter (":") is rejected outright - such files are never
            # imported, not even under a stripped name.
            norm = member.replace("\\", "/")
            segments = [s for s in norm.split("/") if s]
            if (not segments or any(s == ".." for s in segments)
                    or ":" in member or len(segments) > 1 and segments[0].endswith(":")):
                skipped += 1
                continue
            name = segments[-1]
            ext = os.path.splitext(name)[1].lower()
            if not name or ext not in _CC_PHOTO_ALLOWED_EXT:
                skipped += 1
                continue
            try:
                info = zf.getinfo(member)
            except Exception:
                skipped += 1
                continue
            if not info or info.file_size <= 0:
                skipped += 1
                continue
            # Decompressed-size budget (zip-bomb guard): the ZIP metadata
            # claims file_size bytes when expanded - reject the entry (and
            # stop early once the whole budget is gone) without ever holding
            # more than one chunk in RAM.
            if total_written + int(info.file_size) > int(max_total_bytes):
                skipped += 1
                continue
            target = os.path.abspath(os.path.join(directory, name))
            # Belt & braces: resolved target MUST stay inside the folder.
            if not (target == dir_abs or target.startswith(dir_abs + os.sep)):
                skipped += 1
                continue
            # PER-ENTRY isolation: reserved names (con.jpg), illegal
            # filename chars, disk-full or an in-flight-upload lock fail
            # ONLY this entry.
            target_touched = False
            try:
                with zf.open(member, "r") as src, open(target, "wb") as fh:
                    # ``zf.open`` runs FIRST in this with-statement: a corrupt
                    # / encrypted member raises BEFORE ``open(target)`` ever
                    # truncates - a previous good file with the same name must
                    # survive. Only once the target is open (truncated) do we
                    # consider it ours to clean up on failure.
                    target_touched = True
                    while True:
                        chunk = src.read(_CC_ZIP_CHUNK_SIZE)
                        if not chunk:
                            break
                        # BUDGET ENFORCED ON ACTUAL BYTES (not the declared
                        # file_size): a crafted ZIP can LIE in its metadata
                        # (declare 10 bytes, decompress to gigabytes) - the
                        # in-loop check makes that impossible to exploit.
                        if total_written + len(chunk) > int(max_total_bytes):
                            raise IOError("decompressed size budget exceeded")
                        fh.write(chunk)
                        total_written += len(chunk)
                imported += 1
            except Exception:
                skipped += 1
                # Remove a half-written file ONLY if this import actually
                # opened/truncated it - never delete the previous good file
                # that the failing member was about to overwrite.
                if target_touched:
                    try:
                        if os.path.exists(target):
                            os.remove(target)
                    except OSError:
                        pass
    set_country_photo_cursor(country_code, 0)
    _mark_dirty("countries", country_code)
    save_storage()
    return imported, skipped


def remove_country_photos(country_code: str) -> int:
    """Delete every stored photo file of ONE country. Returns count removed."""
    directory = get_country_photos_dir(country_code, create=False)
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for name in os.listdir(directory):
        if os.path.splitext(name)[1].lower() in _CC_PHOTO_ALLOWED_EXT:
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError:
                pass
    if country_code in _storage.get("countries", {}):
        _storage["countries"][country_code]["photo_cursor"] = 0
        _mark_dirty("countries", country_code)
        save_storage()
    return removed


def get_country_photos_used(country_code: str) -> int:
    """How many photos have been handed out since the last (re)import."""
    return get_country_photo_cursor(country_code)

def get_open_countries() -> List[str]:
    """Get list of open countries."""
    return [code for code, data in _storage["countries"].items() if data.get("open", True)]

def get_country_fallback_chain(country_code: str) -> List[str]:
    """
    Get fallback chain for a country code.
    Creates a progressive fallback from most specific to least specific.

    Order:
      1. The exact code itself.
      2. Other sub-codes of the SAME real territory (e.g. +1658 -> +1876, both
         Jamaica) so same-country rates are preferred over the base code.
      3. Base country-code parents (e.g. +1, +262) via prefix rules.

    Explicitly does NOT let Kazakhstan codes (+76/+77) fall back to +7
    (Russia): the ITU +7 block is split between two sovereign countries and
    falling back to the +7 base rate would leak Russian pricing onto Kazakh
    numbers.
    """
    from bot.utils.country_data import get_territory_group_codes

    if not country_code or not country_code.startswith('+'):
        return [country_code] if country_code else []

    fallback_chain = [country_code]

    # 1) same-territory sub-codes
    for group_code in get_territory_group_codes(country_code):
        if group_code != country_code and group_code not in fallback_chain:
            fallback_chain.append(group_code)

    # 2) base-code parents
    if country_code.startswith('+1') and len(country_code) > 2:
        fallback_chain.append('+1')
    elif country_code in ('+76', '+77'):
        # Kazakhstan - NEVER fall back to +7 (Russia)
        pass
    elif country_code.startswith('+7') and len(country_code) > 2:
        fallback_chain.append('+7')
    elif country_code.startswith('+262') and len(country_code) > 4:
        fallback_chain.append('+262')
    elif country_code.startswith('+590') and len(country_code) > 4:
        fallback_chain.append('+590')
    elif country_code.startswith('+599') and len(country_code) > 4:
        fallback_chain.append('+599')
    elif country_code.startswith('+44') and len(country_code) > 3:
        fallback_chain.append('+44')
    elif country_code == '+6721':
        # Australian Antarctic Territory: administered under the Australian
        # numbering plan; must NOT fall back to +672 (Norfolk Island), a
        # different territory sharing the +672 block.
        fallback_chain.append('+61')
    elif country_code.startswith('+672') and len(country_code) > 4:
        fallback_chain.append('+672')
        fallback_chain.append('+61')
    elif country_code.startswith('+61') and len(country_code) > 3:
        fallback_chain.append('+61')
    elif country_code.startswith('+358') and len(country_code) > 4:
        fallback_chain.append('+358')

    return fallback_chain

def find_country_data_with_fallback(country_code: str):
    """
    Find country data using fallback chain.
    
    Args:
        country_code: Country code like +1829
    
    Returns:
        tuple: (matched_code, country_data) or (None, None) if not found
    """
    fallback_chain = get_country_fallback_chain(country_code)
    
    for code in fallback_chain:
        if code in _storage["countries"]:
            return code, _storage["countries"][code]
    
    return None, None

def is_country_open(country_code: str) -> bool:
    """Check if a country is open."""
    matched_code, country_data = find_country_data_with_fallback(country_code)
    if country_data:
        return country_data.get("open", False)
    return False

def validate_rate(rate) -> float:
    """Validate and sanitize rate value."""
    try:
        rate = float(rate)
        if rate < 0:
            return 0.0
        if rate > 10000:
            return 10000.0
        return round(rate, 2)
    except (TypeError, ValueError):
        return 0.0

def validate_capacity(capacity) -> int:
    """Validate and sanitize a country account capacity value.

    ``0`` means unlimited (no cap) - the admin did not set an account limit
    for this country. Any positive integer becomes the max number of accounts
    that may be verified for the country. Invalid/negative input falls back to
    ``0`` (unlimited).
    """
    try:
        value = int(capacity)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    return value

def validate_rates_hierarchy(spam_free_rate, spam_rate, limit_rate):
    """Validate rate hierarchy (spam_free >= limit >= spam).

    spam_free = good/clean account (highest price), limit = limited account
    (middle price), spam = spam account (lowest price).
    """
    spam_free_rate = validate_rate(spam_free_rate)
    spam_rate = validate_rate(spam_rate)
    limit_rate = validate_rate(limit_rate)
    
    if limit_rate > spam_free_rate:
        limit_rate = spam_free_rate
    if spam_rate > limit_rate:
        spam_rate = limit_rate
        
    return spam_free_rate, spam_rate, limit_rate

def get_country_rate(country_code: str, account_status: str = "spam_free") -> Optional[float]:
    """Get rate for a country based on account status.

    Returns None when neither the country nor its fallback chain has any
    configured rates, i.e. no rate is available at all. A configured rate of
    0.0 is a VALID rate (admin set it to 0 = free/no payment) and is returned
    as-is.
    """
    matched_code, country_data = find_country_data_with_fallback(country_code)
    
    if not country_data:
        return None
    
    if account_status == "spam":
        rate = country_data.get("spam_rate", country_data.get("rate", 0.0))
    elif account_status == "limit":
        rate = country_data.get("limit_rate", country_data.get("rate", 0.0))
    else:
        rate = country_data.get("spam_free_rate", country_data.get("rate", 0.0))
    
    return validate_rate(rate)

def get_country_rates(country_code: str) -> Dict[str, float]:
    """Get all rates for a country (spam_free, spam, limit)."""
    matched_code, country_data = find_country_data_with_fallback(country_code)
    
    if not country_data:
        country_data = {}
    
    default_rate = country_data.get("rate", 0.0)
    return {
        "spam_free_rate": country_data.get("spam_free_rate", default_rate),
        "spam_rate": country_data.get("spam_rate", default_rate),
        "limit_rate": country_data.get("limit_rate", default_rate)
    }

def get_user_effective_rate(user_id, country_code: str, account_status: str = "spam_free") -> Optional[float]:
    """Get the effective verification rate for a user.

    A per-user custom rate (custom_rates, set via the admin panel's
    "Set Individual User Rate") overrides the country default for that user,
    regardless of account status.

    The match is EXACT: a custom rate set for a specific country code (e.g.
    +1) applies ONLY to that exact country code, never to its fallback
    sub-codes (e.g. +1876). To give a user a custom rate for another country,
    the admin must set that country's code explicitly.

    Falls back to the country rate when no exact custom rate is set, which
    may be None when the country has no configured rates at all.
    """
    user = _storage["users"].get(str(user_id))
    if user:
        custom_rates = user.get("custom_rates")
        if isinstance(custom_rates, dict) and custom_rates:
            custom_rate = custom_rates.get(country_code)
            if custom_rate is not None:
                return validate_rate(custom_rate)
    return get_country_rate(country_code, account_status)

def get_country_timeout(country_code: str) -> int:
    """Get timeout for a country in seconds.

    Always returns an ``int``: stored values are coerced defensively so a
    non-numeric/string value in storage (e.g. from an old config) can never
    leak into the verification deadline math as a string.
    """
    matched_code, country_data = find_country_data_with_fallback(country_code)
    if country_data:
        try:
            return int(country_data.get("timeout", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT_SECONDS
    return DEFAULT_TIMEOUT_SECONDS

def get_country_capacity(country_code: str) -> int:
    """Get the max account capacity configured for a country.

    ``0`` means unlimited (no explicit account cap was set). The capacity is
    inherited through the country fallback chain (a sub-code without its own
    entry falls back to its parent/base code, same as rates/timeout).
    """
    matched_code, country_data = find_country_data_with_fallback(country_code)
    if country_data:
        try:
            return int(country_data.get("capacity", DEFAULT_COUNTRY_CAPACITY) or DEFAULT_COUNTRY_CAPACITY)
        except (TypeError, ValueError):
            return DEFAULT_COUNTRY_CAPACITY
    return DEFAULT_COUNTRY_CAPACITY

def set_country_rate(country_code: str, rate: float) -> None:
    """Set default rate for a country."""
    if country_code not in _storage["countries"]:
        _storage["countries"][country_code] = {
            "rate": rate, 
            "spam_free_rate": rate,
            "spam_rate": rate,
            "limit_rate": rate,
            "capacity": 0,
            "timeout": 300, 
            "open": True,
            "permanently_closed": False
        }
    else:
        _storage["countries"][country_code]["rate"] = rate
        if "spam_free_rate" not in _storage["countries"][country_code]:
            _storage["countries"][country_code]["spam_free_rate"] = rate
        if "spam_rate" not in _storage["countries"][country_code]:
            _storage["countries"][country_code]["spam_rate"] = rate
        if "limit_rate" not in _storage["countries"][country_code]:
            _storage["countries"][country_code]["limit_rate"] = rate
        _storage["countries"][country_code].setdefault("capacity", 0)
        _storage["countries"][country_code]["open"] = True
        _storage["countries"][country_code]["permanently_closed"] = False
    _mark_dirty("countries", country_code)
    save_storage()

def set_country_rates(
    country_code: str,
    spam_free_rate: float,
    spam_rate: float,
    limit_rate: float,
    capacity: int = None,
    timeout: int = None,
) -> Dict[str, Any]:
    """Set all 3 rates for a country, plus optional capacity and timeout.

    When ``capacity`` is None (legacy / single-country setrate) the existing
    stored capacity is kept; for a brand-new country this defaults to ``0``
    (unlimited).  When ``timeout`` is None the existing stored timeout is kept;
    for a brand-new country this defaults to ``300``.

    Capacity is the ONLY way the admin re-opens a filled country: the SUCCESS
    counter is persistent (never auto-resets at midnight), so an explicitly
    given capacity resets the counter and starts a fresh gate. A rate-only
    update (``capacity=None``) deliberately does NOT touch the counter - a
    filled country stays blocked until capacity is given again.
    """
    spam_free_rate, spam_rate, limit_rate = validate_rates_hierarchy(
        spam_free_rate, spam_rate, limit_rate
    )
    
    if country_code not in _storage["countries"]:
        _capacity = DEFAULT_COUNTRY_CAPACITY if capacity is None else validate_capacity(capacity)
        try:
            _timeout = DEFAULT_TIMEOUT_SECONDS if timeout is None else int(timeout)
        except (TypeError, ValueError):
            _timeout = DEFAULT_TIMEOUT_SECONDS
        _storage["countries"][country_code] = {
            "rate": spam_free_rate,
            "spam_free_rate": spam_free_rate,
            "spam_rate": spam_rate,
            "limit_rate": limit_rate,
            "capacity": _capacity,
            "timeout": _timeout,
            "open": True,
            "permanently_closed": False
        }
    else:
        _storage["countries"][country_code]["rate"] = spam_free_rate
        _storage["countries"][country_code]["spam_free_rate"] = spam_free_rate
        _storage["countries"][country_code]["spam_rate"] = spam_rate
        _storage["countries"][country_code]["limit_rate"] = limit_rate
        if capacity is not None:
            _storage["countries"][country_code]["capacity"] = validate_capacity(capacity)
        else:
            _storage["countries"][country_code].setdefault("capacity", DEFAULT_COUNTRY_CAPACITY)
        if timeout is not None:
            try:
                _storage["countries"][country_code]["timeout"] = int(timeout)
            except (TypeError, ValueError):
                _storage["countries"][country_code].setdefault("timeout", DEFAULT_TIMEOUT_SECONDS)
        else:
            _storage["countries"][country_code].setdefault("timeout", DEFAULT_TIMEOUT_SECONDS)
        _storage["countries"][country_code]["open"] = True
        _storage["countries"][country_code]["permanently_closed"] = False

    # Admin explicitly gave a capacity -> reset the persistent SUCCESS gate so
    # the country re-opens with a fresh count. Without this the filled counter
    # would keep the country blocked forever after the first fill.
    if capacity is not None:
        try:
            from bot.data.counters import reset_daily_success_count
            reset_daily_success_count(country_code)
        except Exception:
            pass

    _mark_dirty("countries", country_code)
    save_storage_now()
    
    result = {
        "spam_free_rate": spam_free_rate,
        "spam_rate": spam_rate,
        "limit_rate": limit_rate,
        "capacity": _storage["countries"][country_code].get("capacity", DEFAULT_COUNTRY_CAPACITY),
        "timeout": _storage["countries"][country_code].get("timeout", DEFAULT_TIMEOUT_SECONDS),
        "validated": True
    }

    return result

def set_country_rate_by_type(country_code: str, rate_type: str, rate: float) -> float:
    """Set specific rate type for a country.

    F-1 FIX: the old version stored ANY numeric value with no sign check and
    no hierarchy re-clamp - a negative rate reduced user balances per session
    and limit > spam_free broke the pricing ladder. Now: the rate must be a
    finite non-negative number (ValueError otherwise) and the full
    free >= limit >= spam hierarchy is re-clamped after the edit.

    Returns:
        The stored (validated) rate.
    """
    if rate_type not in ("spam_free_rate", "spam_rate", "limit_rate"):
        raise ValueError(f"Unknown rate type: {rate_type!r}")
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        raise ValueError(f"Rate must be a number, got {rate!r}")
    if rate_f != rate_f or rate_f == float("inf") or rate_f < 0:
        raise ValueError("Rate must be a non-negative finite number")
    rate_f = validate_rate(rate_f)

    with _storage_lock:
        if country_code not in _storage["countries"]:
            _storage["countries"][country_code] = {
                "rate": rate_f,
                "spam_free_rate": rate_f,
                "spam_rate": rate_f,
                "limit_rate": rate_f,
                "capacity": 0,
                "timeout": 300,
                "open": True,
                "permanently_closed": False
            }
        else:
            data = _storage["countries"][country_code]
            default_rate = data.get("rate") or rate_f
            if "spam_free_rate" not in data:
                data["spam_free_rate"] = default_rate
            if "spam_rate" not in data:
                data["spam_rate"] = default_rate
            if "limit_rate" not in data:
                data["limit_rate"] = default_rate
            data.setdefault("capacity", 0)
            data[rate_type] = rate_f
            if rate_type == "spam_free_rate":
                data["rate"] = rate_f
            data["open"] = True
            data["permanently_closed"] = False
            # F-1: re-clamp the ladder after the single-slot edit so the
            # stored config can never violate free >= limit >= spam.
            free, spam, limit = validate_rates_hierarchy(
                data.get("spam_free_rate"),
                data.get("spam_rate"),
                data.get("limit_rate"),
            )
            data["spam_free_rate"], data["spam_rate"], data["limit_rate"] = free, spam, limit
            if rate_type == "spam_free_rate":
                data["rate"] = free
            rate_f = data[rate_type]
    _mark_dirty("countries", country_code)
    save_storage_now()
    return rate_f

def set_session_timeout(timeout_seconds: int) -> None:
    """Set session timeout globally."""
    for country in _storage["countries"]:
        _storage["countries"][country]["timeout"] = timeout_seconds
        _mark_dirty("countries", country)
    save_storage()

def set_country_timeout(country_code: str, timeout_seconds: int):
    """Set session timeout for a specific country."""
    if country_code in _storage["countries"]:
        _storage["countries"][country_code]["timeout"] = timeout_seconds
        _mark_dirty("countries", country_code)
        save_storage()
        return True, f"⏳ Timeout for {country_code} set to {timeout_seconds} seconds"
    else:
        return False, f"❌ Country {country_code} not found. Set rate first!"

def add_open_country(country_code: str) -> None:
    """Add a country to open list."""
    if country_code not in _storage["countries"]:
        _storage["countries"][country_code] = {"rate": 0.0, "capacity": 0, "timeout": 300, "open": True}
    else:
        _storage["countries"][country_code]["open"] = True
    _mark_dirty("countries", country_code)
    save_storage()

def close_country(country_code: str) -> None:
    """Close a country."""
    if country_code in _storage["countries"]:
        _storage["countries"][country_code]["open"] = False
        _mark_dirty("countries", country_code)
        save_storage()

def toggle_country_status(country_code: str):
    """Toggle country open/close status.

    Closing a country via the toggle is PERMANENT: the country is marked
    ``permanently_closed`` so bulk operations (e.g. "Enable All Configured")
    never re-open it. To re-enable the country the admin must set a fresh
    price (which clears the permanent-close flag). Opening via the toggle
    explicitly clears the flag.
    """
    if country_code in _storage["countries"]:
        current = _storage["countries"][country_code].get("open", False)
        new_status = not current
        _storage["countries"][country_code]["open"] = new_status
        _storage["countries"][country_code]["permanently_closed"] = not new_status
        _mark_dirty("countries", country_code)
        save_storage()
        return country_code, new_status
    return None, None

def disable_all_countries() -> int:
    """Disable all countries. Returns count of affected countries."""
    count = 0
    for code in _storage["countries"]:
        if _storage["countries"][code].get("open", False):
            _storage["countries"][code]["open"] = False
            _mark_dirty("countries", code)
            count += 1
    save_storage()
    return count

def enable_all_countries() -> Dict[str, int]:
    """Open every configured country that is not permanently closed.

    E-5 FIX: the handler previously mutated ``open=True`` directly and called
    ``save_storage()`` WITHOUT ``_mark_dirty`` - the incremental sync wrote
    nothing, so a restart silently rolled every country back to closed.
    Mirror of ``disable_all_countries``: mark each changed row dirty (and
    money/state-critical, so persist immediately with ``save_storage_now``).

    Permanently-closed countries (Toggle Country) are skipped - only setting
    a new price re-enables them.

    Returns:
        {"enabled": n, "skipped": m}
    """
    enabled = 0
    skipped = 0
    for code, data in _storage.get("countries", {}).items():
        if not isinstance(data, dict):
            continue
        if data.get("permanently_closed", False):
            skipped += 1
            continue
        if not data.get("open", False):
            data["open"] = True
            _mark_dirty("countries", code)
            enabled += 1
    save_storage_now()
    return {"enabled": enabled, "skipped": skipped}

def is_country_permanently_closed(country_code: str) -> bool:
    """True when a country was permanently closed via the toggle.

    Permanently-closed countries must never be re-opened by bulk operations
    (e.g. "Enable All Configured"); only setting a new price re-enables them.
    """
    data = _storage["countries"].get(country_code)
    return bool(data and data.get("permanently_closed", False))

def delete_all_open_countries() -> int:
    """Permanently remove every currently-open country from storage.

    Only countries whose ``open`` is True are removed; closed and
    permanently-closed countries are left untouched. Returns the number of
    countries deleted. There is NO undo for this operation.
    """
    to_delete = [
        code for code, data in _storage["countries"].items()
        if isinstance(data, dict) and data.get("open", False)
    ]
    for code in to_delete:
        del _storage["countries"][code]
    if to_delete:
        _mark_dirty("countries", None)  # deletions → full table sync
        save_storage_now()
    return len(to_delete)

def is_all_countries_paused() -> bool:
    """Check if all countries are temporarily paused (master switch is OFF).

    When True every country reports closed, so no new session is accepted.
    """
    return _storage.get("all_countries_paused", False)

def toggle_all_countries_pause() -> bool:
    """Toggle the temporary all-countries master switch.

    Pausing (ON -> OFF): snapshots the current open/closed state of every
    country into ``countries_pause_snapshot`` and then closes ALL countries.

    Unpausing (OFF -> ON): restores every country from the saved snapshot, so
    each country comes back exactly as it was before the pause.

    This is intentionally different from :func:`disable_all_countries`, which
    permanently closes every country with no way to restore.

    Returns:
        The new paused state (True = all countries temporarily closed).
    """
    _storage.setdefault("countries", {})
    paused = _storage.get("all_countries_paused", False)

    if not paused:
        snapshot = {}
        for code, data in _storage["countries"].items():
            snapshot[code] = bool(data.get("open", False))
        _storage["countries_pause_snapshot"] = snapshot
        for code in _storage["countries"]:
            _storage["countries"][code]["open"] = False
            _mark_dirty("countries", code)
        _storage["all_countries_paused"] = True
    else:
        snapshot = _storage.get("countries_pause_snapshot", {})
        for code in _storage["countries"]:
            _storage["countries"][code]["open"] = bool(snapshot.get(code, False))
            _mark_dirty("countries", code)
        _storage["all_countries_paused"] = False

    # S1: row-level settings persistence for the two pause-state keys.
    _mark_settings_key_dirty("countries_pause_snapshot")
    _mark_settings_key_dirty("all_countries_paused")
    save_storage()
    return _storage["all_countries_paused"]
