"""Session file export: write JSON + SESSION files and send them to the channel."""
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from bot import config
from bot.services.channel_sender import send_to_channel_with_retry
from bot.utils.device_generator import generate_device_info

logger = logging.getLogger(__name__)

_FERNET_CACHE = {"key": None, "fernet": None}

# ------------------------------------------------------------------
# H8 fix: O(N) directory scans must never run per-rejection on the
# event loop. Two mechanisms replace the old inline listdir:
#   1. DIRECT-path deletion for every deterministic artifact name the
#      bot writes (the callers can also pass exact filenames persisted
#      in the pending row).
#   2. A single background worker that drains a phone queue with ONE
#      combined directory scan per drain, catching any unknown/legacy
#      variant (e.g. `_pending_pyrogram_<hex>.session` suffixes).
# Cleanup stays complete; the event loop and the rejection latency stay O(1).
# ------------------------------------------------------------------
_SCAN_QUEUE: "set[str]" = set()
_SCAN_QUEUE_LOCK = threading.Lock()
_SCAN_WORKER_STARTED = False
_SCAN_INTERVAL_SECONDS = 2.0
_SCAN_FOLDERS = ("sessions", "verified_sessions")


def _register_scan_targets(digits: str) -> None:
    global _SCAN_WORKER_STARTED
    with _SCAN_QUEUE_LOCK:
        _SCAN_QUEUE.add(digits)
        already = _SCAN_WORKER_STARTED
        _SCAN_WORKER_STARTED = True
    if not already:
        t = threading.Thread(target=_scan_worker_loop, name="session-artifact-scanner", daemon=True)
        t.start()


def _scan_worker_loop() -> None:
    """Drain queued phones; one listdir per folder per drain (amortized O(1))."""
    while True:
        time.sleep(_SCAN_INTERVAL_SECONDS)
        with _SCAN_QUEUE_LOCK:
            if not _SCAN_QUEUE:
                continue
            batch = set(_SCAN_QUEUE)
            _SCAN_QUEUE.clear()
        try:
            for folder in _SCAN_FOLDERS:
                try:
                    if not os.path.isdir(folder):
                        continue
                    for name in os.listdir(folder):
                        if not name.endswith((".session", ".session-journal")):
                            continue
                        for digits in batch:
                            if digits in name:
                                path = os.path.join(folder, name)
                                try:
                                    os.remove(path)
                                except OSError:
                                    pass
                                break
                except OSError:
                    continue
        except Exception:
            logger.debug("artifact scan worker error", exc_info=True)


def _try_remove(path: str) -> bool:
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


def _get_mirror_fernet(secret: str):
    """Fernet for the encrypted verified_sessions mirror (cached per key)."""
    if not secret:
        return None
    if _FERNET_CACHE["key"] == secret and _FERNET_CACHE["fernet"] is not None:
        return _FERNET_CACHE["fernet"]
    try:
        import base64
        import hashlib
        from cryptography.fernet import Fernet
        derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        fernet = Fernet(derived)
        _FERNET_CACHE["key"] = secret
        _FERNET_CACHE["fernet"] = fernet
        return fernet
    except ImportError:
        logger.warning("cryptography not installed - verified_sessions mirror disabled")
        return None
    except Exception:
        logger.warning("verified_sessions mirror fernet init failed", exc_info=True)
        return None


def load_verified_session_json(phone: str) -> Optional[str]:
    """Read the local verified-session JSON mirror for a phone (P6-60).

    Handles BOTH layouts: the legacy plaintext ``<digits>.json`` and the
    encrypted ``<digits>.json.enc`` (decrypted with SESSION_SECRET). Returns
    the JSON TEXT, or None when absent/unreadable. Never raises.
    """
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        return None
    enc_path = f"verified_sessions/{digits}.json.enc"
    plain_path = f"verified_sessions/{digits}.json"
    try:
        if os.path.exists(enc_path):
            secret = getattr(config, "SESSION_SECRET", "") or ""
            fernet = _get_mirror_fernet(secret)
            if fernet is None:
                logger.warning("Encrypted session mirror exists but no SESSION_SECRET - cannot read %s", enc_path)
                return None
            with open(enc_path, "r", encoding="utf-8") as f:
                return fernet.decrypt(f.read().encode()).decode()
        if os.path.exists(plain_path):
            with open(plain_path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        logger.warning("load_verified_session_json failed for %s", phone, exc_info=True)
    return None


def delete_verified_session_artifacts(phone: str, extra_files: Optional[list] = None) -> int:
    """Delete the local mirror artifacts of a phone (P6-60 reject cleanup).

    Removed: ``verified_sessions/<digits>.json`` (legacy plaintext mirror),
    ``verified_sessions/<digits>.json.enc`` (encrypted mirror) and any raw
    ``.session`` file for this phone in sessions/ + verified_sessions/.

    H8: the O(N) directory scan no longer runs INLINE per rejection - the
    direct deterministic paths (plus any exact filenames the caller passes
    from the pending row) are removed synchronously in O(1), and a single
    background worker sweeps both folders for unknown/legacy variants.

    ``extra_files``: exact artifact paths/names known to the caller (e.g. the
    pending row's ``session_file`` / ``telethon_file``), deleted immediately.

    Returns the number of files deleted synchronously. Never raises.
    """
    removed = 0
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        return 0

    base_names = [
        f"{digits}.session",
        f"{digits}_pyrogram.session",
        f"{digits}_telethon.session",
        f"{digits}_verified_pyrogram.session",
        f"{digits}_verified_telethon.session",
    ]
    targets = [
        f"verified_sessions/{digits}.json",
        f"verified_sessions/{digits}.json.enc",
    ]
    for folder in _SCAN_FOLDERS:
        for name in base_names:
            targets.append(os.path.join(folder, name))
        # sqlite sidecar files accompany every .session
        for name in list(base_names):
            targets.append(os.path.join(folder, f"{name}-journal"))

    for extra in (extra_files or []):
        if not extra:
            continue
        name = os.path.basename(str(extra))
        targets.append(str(extra))
        if not os.path.isabs(str(extra)):
            for folder in _SCAN_FOLDERS:
                targets.append(os.path.join(folder, name))
                targets.append(os.path.join(folder, f"{name}-journal"))
        targets.append(f"{str(extra)}-journal")
        targets.append(os.path.join("verified_sessions", f"{name}-journal"))

    seen = set()
    for path in targets:
        if path in seen:
            continue
        seen.add(path)
        if _try_remove(path):
            removed += 1

    # Background sweep for anything the deterministic list cannot name
    # (timestamped/uuid variants from other code paths, legacy files).
    _register_scan_targets(digits)
    return removed


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a value to int, NEVER raising.

    The account's ``user_id`` can arrive as None, an empty string, or a
    non-numeric string (incomplete resolution for some accounts). A raw
    ``int(...)`` on those raises, and because build_session_data_full runs
    inside send_json_alongside AFTER the .session file was already sent, the
    exception is swallowed and the buyer receives the session WITHOUT its JSON
    ("session goes, JSON does not"). Coercing safely guarantees the JSON is
    always built and delivered.
    """
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _json_safe(value, default='null'):
    """Return a JSON-serializable representation of ``value``, NEVER raising.

    The account metadata can carry live Pyrogram objects — most notably
    ``session_info['photo']`` is a ``ChatPhoto``, which ``json.dumps`` cannot
    serialize ("Object of type ChatPhoto is not JSON serializable"). That
    exception was raised INSIDE send_json_alongside AFTER the .session file was
    already sent, so it was swallowed and the buyer got the session WITHOUT the
    JSON. For media/photo objects we keep the useful file id; anything else is
    stringified; on any failure we fall back to ``default``.
    """
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        return value
    for attr in ('big_file_id', 'file_id', 'small_file_id'):
        fid = getattr(value, attr, None)
        if isinstance(fid, str) and fid:
            return fid
    try:
        return str(value)
    except Exception:
        return default


def build_session_data_full(
    phone: str,
    session_info: Dict[str, Any],
    session_string: str,
    twofa_password: str,
    country_code: str,
    rate: float,
    account_status: str,
    status_details: Any,
    user_id: str,
    device_info: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the comprehensive session data structure with account status."""
    # Resolve the REAL API that created this session (parsed from the session
    # string and matched against the configured API pool) so the JSON never
    # stamps the primary API when the session was built with a rotated one.
    from bot.services.session_formats import resolve_session_api
    _api_id, _api_hash = resolve_session_api(session_string)
    return {
        "session_file": phone,
        "user_id": _safe_int(session_info.get('user_id', 0)),
        "phone": phone,
        "register_time": int(datetime.now().timestamp()),
        "session_str": session_string,
        "app_id": _api_id,
        "app_hash": _api_hash,
        "sdk": device_info.get('sdk'),
        "app_version": device_info.get('app_version'),
        "device": device_info.get('device'),
        "device_token": device_info.get('device_token'),
        "device_token_secret": device_info.get('device_token_secret'),
        "device_secret": device_info.get('device_secret'),
        "signature": device_info.get('signature'),
        "certificate": device_info.get('certificate'),
        "safetynet": device_info.get('safetynet'),
        "perf_cat": device_info.get('perf_cat'),
        "tz_offset": device_info.get('tz_offset'),
        "last_check_time": int(datetime.now().timestamp()),
        "avatar": _json_safe(session_info.get('photo', 'null')),
        "first_name": session_info.get('first_name', ''),
        "last_name": session_info.get('last_name', ''),
        "username": session_info.get('username'),
        "sex": session_info.get('sex', 0),
        "lang_code": session_info.get('language_code', 'en'),
        "system_lang_code": session_info.get('system_lang_code', 'en-US'),
        "twoFA": twofa_password,
        "ipv6": device_info.get('ipv6'),
        "module": "AddAccount",
        # The client that actually created the session is Pyrogram (the
        # "program" format); this JSON is generated from a Pyrogram session
        # string, so the field must name the real program, not a placeholder
        # URL. Buyers' tools key off this value to pick the right login tool.
        "program": "Pyrogram",
        "country_code": country_code,
        "rate": rate,
        "account_status": account_status,
        "status_details": status_details,
        "verified_at": datetime.now().isoformat(),
        "verified_by_user": user_id
    }


async def write_and_send_session_files(
    session_id: str,
    phone: str,
    user_id: str,
    session_info: Dict[str, Any],
    session_string: str,
    twofa_password: str,
    country_code: str,
    rate: float,
    account_status: str,
    status_details: Any,
    caption: str = "",
) -> Tuple[bool, bool]:
    """Write session JSON files and send them honoring the admin format selection.

    Unlike the old implementation (which hardcoded Telethon + JSON to
    CHANNEL_VERIFIED and ignored the admin's Session Management format
    choices), this now delegates to ``report_success_session`` so every
    enabled format — telethon, pyrogram, tdata, txt — goes to its own
    channel, exactly as the admin configured.

    Returns ``(files_sent, io_error)`` where ``io_error=True`` means the caller
    must ``continue`` to the next pending session.
    """
    from bot.services.session_reporting import (
        _all_attempted_formats_delivered,
        report_success_session,
    )

    # Generate the JSON file for device info (non-fatal side-effect).
    device_info = generate_device_info()
    session_data_full = build_session_data_full(
        phone=phone,
        session_info=session_info,
        session_string=session_string,
        twofa_password=twofa_password,
        country_code=country_code,
        rate=rate,
        account_status=account_status,
        status_details=status_details,
        user_id=user_id,
        device_info=device_info,
    )
    session_json = json.dumps(session_data_full, indent=2, ensure_ascii=False, default=str)

    # The local JSON write below is a NON-FATAL side-effect: the real JSON is
    # delivered by report_success_session (send_json_alongside) straight to the
    # channel. A transient disk error here must NEVER kill the whole success
    # report (it previously returned io_error=True and validation.py skipped
    # every channel delivery for a single bad disk write).
    #
    # P6-60: the local mirror previously stored session_str + twoFA in
    # PLAINTEXT. When SESSION_SECRET is set the mirror is now Fernet-encrypted
    # (channel JSON keeps its plaintext form - it is the product the buyer
    # receives); without a key the plaintext mirror is skipped entirely
    # (fail-closed: no key, no plaintext secrets on disk).
    try:
        from bot import config as _cfg
        _fernet = _get_mirror_fernet(_cfg.SESSION_SECRET)
        if _fernet is not None:
            os.makedirs("verified_sessions", exist_ok=True)
            json_file = f"verified_sessions/{phone.replace('+', '')}.json.enc"
            with open(json_file, 'w', encoding='utf-8') as f:
                f.write(_fernet.encrypt(session_json.encode()).decode())
        else:
            logger.info(
                "verified_sessions mirror skipped: SESSION_SECRET not set "
                "(plaintext secrets are never written to disk)"
            )
    except IOError as e:
        logger.warning("Local JSON write failed for %s (non-fatal, continuing): %s",
                       phone, e)

    # Delegate to the per-format reporter so the admin's format selection is
    # honoured (fixes the old bug where only a hardcoded Telethon file went to
    # CHANNEL_VERIFIED and TData selected by the admin was silently dropped).
    # The whole dispatch is retried up to 3 times so a transient channel
    # failure can never permanently lose the session files (a text report is
    # NEVER substituted for a file).
    from bot.bootstrap.client import app as _bot_client
    outcomes = []
    for _attempt in range(1, 4):
        outcomes = await report_success_session(
            bot_client=_bot_client,
            phone=phone,
            user_id=int(user_id or 0),
            session_info=session_info,
            session_string=session_string,
            twofa_password=twofa_password,
            country_code=country_code,
            rate=rate,
            account_status=account_status or "spam_free",
            status_details=status_details or {},
            success_report=caption,
            telethon_file=None,
            pyrogram_file=None,
            session_id=session_id,
        )
        files_sent = _all_attempted_formats_delivered(outcomes)
        if files_sent:
            break
        if _attempt < 3:
            import asyncio as _asyncio
            logger.warning("write_and_send_session_files: report attempt %d/3 failed for %s - retrying",
                           _attempt, phone)
            await _asyncio.sleep(min(2.0 * _attempt, 6.0))
    if not files_sent:
        logger.warning(
            "write_and_send_session_files: no format report delivered for %s "
            "(outcomes=%s) - check the per-format channel configuration; "
            "channel_report_pending will be set so the monitor safety net retries",
            phone, outcomes)
    return files_sent, False
