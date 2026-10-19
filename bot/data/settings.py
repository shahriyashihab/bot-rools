"""Refactored from bot.utils.storage: Global settings storage operations."""
from typing import Optional
from datetime import datetime
from bot.data.state import _mark_dirty, _mark_settings_key_dirty, _do_save_background, _storage, save_storage, thread_safe

def get_required_channel() -> str:
    """Get required channel. Priority: admin-panel override -> .env (config) -> storage.

    The admin panel override (stored in ``channel_overrides``) wins so that a
    runtime change made from the panel takes effect immediately. When no
    override is set, the .env value wins, then the legacy storage fallback
    (admin panel override from older versions).
    """
    try:
        overrides = _storage.get("channel_overrides") or {}
        if overrides.get("REQUIRED_CHANNEL"):
            return overrides["REQUIRED_CHANNEL"]
    except (AttributeError, TypeError):
        pass
    try:
        from bot.config import REQUIRED_CHANNEL
        if REQUIRED_CHANNEL:
            return REQUIRED_CHANNEL
    except (ImportError, AttributeError):
        pass
    return _storage.get("required_channel", "")

def set_required_channel(channel: str) -> None:
    """Set required channel in storage."""
    _storage["required_channel"] = channel
    _mark_settings_key_dirty("required_channel")
    save_storage()

def get_channel_override(name: str) -> Optional[str]:
    """Get a runtime channel override set from the admin panel, or None."""
    overrides = _storage.get("channel_overrides") or {}
    return overrides.get(name)

def set_channel_override(name: str, value) -> None:
    """Persist a runtime channel override from the admin panel."""
    if "channel_overrides" not in _storage or not isinstance(_storage["channel_overrides"], dict):
        _storage["channel_overrides"] = {}
    _storage["channel_overrides"][name] = value
    _mark_settings_key_dirty("channel_overrides")
    save_storage()
    try:
        from bot.middleware.channel_guard import invalidate_all_memberships
        invalidate_all_memberships()
    except Exception:
        pass

def clear_channel_override(name: str) -> bool:
    """Remove a runtime channel override (fall back to .env config)."""
    overrides = _storage.get("channel_overrides")
    if isinstance(overrides, dict) and name in overrides:
        del overrides[name]
        _mark_settings_key_dirty("channel_overrides")
        save_storage()
        try:
            from bot.middleware.channel_guard import invalidate_all_memberships
            invalidate_all_memberships()
        except Exception:
            pass
        return True
    return False

def is_spam_check_enabled() -> bool:
    """Check if spam check is enabled. Default is True (enabled)."""
    return _storage.get("spam_check_enabled", True)

def toggle_spam_check() -> bool:
    """Toggle spam check enabled/disabled. Returns new state."""
    current = _storage.get("spam_check_enabled", True)
    _storage["spam_check_enabled"] = not current
    _mark_settings_key_dirty("spam_check_enabled")
    save_storage()
    return _storage["spam_check_enabled"]

def is_two_factor_enabled() -> bool:
    """Check if Two-Factor Authentication setup is enabled. Default is True (enabled)."""
    return _storage.get("two_factor_enabled", True)

def toggle_two_factor() -> bool:
    """Toggle Two-Factor Authentication setup on/off. Returns new state."""
    current = _storage.get("two_factor_enabled", True)
    _storage["two_factor_enabled"] = not current
    _mark_settings_key_dirty("two_factor_enabled")
    save_storage()
    return _storage["two_factor_enabled"]

def is_device_check_enabled() -> bool:
    """Check if device check is enabled. Default is True (enabled)."""
    return _storage.get("device_check_enabled", True)

def toggle_device_check() -> bool:
    """Toggle device check on/off. Returns new state."""
    current = _storage.get("device_check_enabled", True)
    _storage["device_check_enabled"] = not current
    _mark_settings_key_dirty("device_check_enabled")
    save_storage()
    return _storage["device_check_enabled"]

def is_same_number_enabled() -> bool:
    """Check if the same-number block is enabled. Default is True (enabled)."""
    return _storage.get("same_number_enabled", True)

def toggle_same_number() -> bool:
    """Toggle the same-number block on/off. Returns new state."""
    current = _storage.get("same_number_enabled", True)
    _storage["same_number_enabled"] = not current
    _mark_settings_key_dirty("same_number_enabled")
    save_storage()
    return _storage["same_number_enabled"]

def get_successful_numbers() -> dict:
    """Get the registry of phone numbers that already completed a SUCCESS session."""
    data = _storage.get("successful_numbers")
    return data if isinstance(data, dict) else {}

def is_number_successful(phone: str) -> bool:
    """Return True when the given phone number already completed a SUCCESS session."""
    if not phone:
        return False
    return phone in get_successful_numbers()

def record_successful_number(phone: str, user_id) -> None:
    """Permanently record a phone number that completed a SUCCESS session.

    Used by the same-number rule: a number that was already successful can
    never be submitted again by any user, so a single account cannot be reused
    to farm repeated credits.

    Persisted as ONE row in the dedicated ``successful_numbers`` table (row
    key = phone), NOT as part of the settings blob - so a growing registry
    never rewrites the whole settings table on every save.

    Runs under ``_storage_lock``: the hourly size-cap prune (counters.
    cleanup_old_data) REBINDS the registry dict object, so an unlocked
    read-mutate-rebind here could resurrect a pruned registry.
    """
    if not phone:
        return
    with _storage_lock:
        registry = get_successful_numbers()
        registry[phone] = {
            "user_id": str(user_id),
            "verified_at": datetime.now().isoformat(),
        }
        _storage["successful_numbers"] = registry
        _mark_dirty("successful_numbers", phone)
    _do_save_background()

def get_auto_approve_enabled() -> bool:
    """Whether successful sessions are automatically approved (no manual approval needed)."""
    return bool(_storage.get("auto_approve_enabled", False))

def set_auto_approve_enabled(enabled: bool) -> None:
    """Persist the auto-approve setting."""
    _storage["auto_approve_enabled"] = bool(enabled)
    _mark_settings_key_dirty("auto_approve_enabled")
    save_storage()

def get_session_expiry_hours() -> int:
    """Hours after which an old stuck/pending session is cleaned up. Default 24."""
    try:
        return int(_storage.get("session_expiry_hours", 24))
    except (TypeError, ValueError):
        return 24

def set_session_expiry_hours(hours: int) -> None:
    """Persist the session expiry (cleanup) threshold in hours."""
    try:
        _storage["session_expiry_hours"] = max(1, int(hours))
    except (TypeError, ValueError):
        _storage["session_expiry_hours"] = 24
    _mark_settings_key_dirty("session_expiry_hours")
    save_storage()

def get_stuck_session_expiry_hours() -> int:
    """Hours after which a stuck session that still has its session string is finalized. Default 72."""
    try:
        return int(_storage.get("stuck_session_expiry_hours", 72))
    except (TypeError, ValueError):
        return 72

ACCOUNT_TIMEZONE_DEFAULT_OFFSET = 0

def get_account_timezone_offset() -> int:
    """UTC offset (whole hours, -12..+14) selected in the admin panel for the
    time shown in the /account command. Defaults to UTC+0.
    """
    try:
        offset = int(_storage.get("account_timezone_offset", ACCOUNT_TIMEZONE_DEFAULT_OFFSET))
    except (TypeError, ValueError):
        return ACCOUNT_TIMEZONE_DEFAULT_OFFSET
    if not -12 <= offset <= 14:
        return ACCOUNT_TIMEZONE_DEFAULT_OFFSET
    return offset

def set_account_timezone_offset(offset: int) -> None:
    """Persist the admin-selected UTC offset (whole hours) for /account time."""
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = ACCOUNT_TIMEZONE_DEFAULT_OFFSET
    if not -12 <= offset <= 14:
        offset = ACCOUNT_TIMEZONE_DEFAULT_OFFSET
    _storage["account_timezone_offset"] = offset
    _mark_settings_key_dirty("account_timezone_offset")
    save_storage()

def get_account_timezone_label() -> str:
    """Human-readable label of the selected timezone, e.g. ``UTC+6`` / ``UTC-5`` / ``UTC+0``."""
    return f"UTC{get_account_timezone_offset():+d}"

def set_stuck_session_expiry_hours(hours: int) -> None:
    """Persist the stuck-session finalize threshold in hours."""
    try:
        _storage["stuck_session_expiry_hours"] = max(1, int(hours))
    except (TypeError, ValueError):
        _storage["stuck_session_expiry_hours"] = 72
    _mark_settings_key_dirty("stuck_session_expiry_hours")
    save_storage()

# ------------------------------------------------------------------
#  /cap capacity UI selection
# ------------------------------------------------------------------

CAPACITY_UI_DEFAULT = 1

def get_capacity_ui() -> int:
    """Return the active /cap capacity UI view id (default 1).

    The admin panel selects which UI the /cap command renders (1 = table,
    2 = banner). Future UIs added in the codebase (3, 4, ...) are simply
    stored here; the /cap dispatcher falls back to UI #1 for any id that
    has no registered builder yet.
    """
    try:
        ui = int(_storage.get("capacity_ui", CAPACITY_UI_DEFAULT))
    except (TypeError, ValueError):
        return CAPACITY_UI_DEFAULT
    if ui < 1:
        return CAPACITY_UI_DEFAULT
    return ui

def set_capacity_ui(ui_id: int) -> int:
    """Persist the active /cap capacity UI view id. Returns the new value.

    Any positive integer is accepted so future UIs can be selected as soon
    as they are registered; ids without a registered builder fall back to
    UI #1 in the /cap dispatcher.
    """
    try:
        ui_id = int(ui_id)
    except (TypeError, ValueError):
        ui_id = CAPACITY_UI_DEFAULT
    if ui_id < 1:
        ui_id = CAPACITY_UI_DEFAULT
    _storage["capacity_ui"] = ui_id
    _mark_settings_key_dirty("capacity_ui")
    save_storage()
    return ui_id


# ------------------------------------------------------------------
#  Session reporting modes (admin panel: Session Management)
# ------------------------------------------------------------------
# Pending & Multidevice modes: the admin picks AT MOST ONE session format
# (telethon / pyrogram / tdata / txt); its report always goes to ONE shared
# channel (CHANNEL_SUBMIT / CHANNEL_24HR_UP).
# Success mode: the admin picks AT LEAST ONE format (min 1, max 4); each
# enabled format is reported to its own channel.
SESSION_FORMATS = ("telethon", "pyrogram", "tdata", "txt")

def get_session_pending_type() -> str:
    """Return the pending-report session format ('' = none selected)."""
    value = _storage.get("session_pending_type", "")
    return value if value in SESSION_FORMATS else ""

def set_session_pending_type(fmt: str) -> str:
    """Set the pending-report format. Empty string clears it. Max ONE."""
    fmt = fmt.strip().lower() if fmt else ""
    _storage["session_pending_type"] = fmt if fmt in SESSION_FORMATS else ""
    _mark_settings_key_dirty("session_pending_type")
    save_storage()
    return _storage["session_pending_type"]

def get_session_multidevice_type() -> str:
    """Return the multidevice-report session format ('' = none selected)."""
    value = _storage.get("session_multidevice_type", "")
    return value if value in SESSION_FORMATS else ""

def set_session_multidevice_type(fmt: str) -> str:
    """Set the multidevice-report format. Empty string clears it. Max ONE."""
    fmt = fmt.strip().lower() if fmt else ""
    _storage["session_multidevice_type"] = fmt if fmt in SESSION_FORMATS else ""
    _mark_settings_key_dirty("session_multidevice_type")
    save_storage()
    return _storage["session_multidevice_type"]

def get_session_success_types() -> list:
    """Return the list of enabled success-report formats (min 1, max 4).

    Defaults to ``["telethon"]`` so existing behaviour (Telethon -> verified
    channel) is preserved even before the admin touches the panel.
    """
    value = _storage.get("session_success_types")
    if not isinstance(value, list):
        return ["telethon"]
    valid = [f for f in value if f in SESSION_FORMATS]
    # Guarantee the "must select at least one" rule.
    if not valid:
        valid = ["telethon"]
    return valid

def set_session_success_types(formats: list) -> list:
    """Persist the enabled success formats. Enforces min 1 (falls back to
    telethon if the list is empty/invalid)."""
    if not isinstance(formats, list):
        formats = []
    valid = [f for f in formats if f in SESSION_FORMATS]
    if not valid:
        valid = ["telethon"]
    _storage["session_success_types"] = valid
    _mark_settings_key_dirty("session_success_types")
    save_storage()
    return valid

def toggle_session_success_type(fmt: str) -> list:
    """Toggle one success format on/off. Returns the new enabled list
    (never empty — the last format cannot be turned off)."""
    fmt = fmt.strip().lower() if fmt else ""
    if fmt not in SESSION_FORMATS:
        return get_session_success_types()
    current = get_session_success_types()
    if fmt in current:
        # Never allow ALL formats to be off (min-1 rule).
        if len(current) == 1:
            return current
        current = [f for f in current if f != fmt]
    else:
        current.append(fmt)
    _storage["session_success_types"] = current
    _mark_settings_key_dirty("session_success_types")
    save_storage()
    return current
