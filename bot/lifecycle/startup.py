"""Startup tasks executed after the bot connects."""
import asyncio
import logging
import os
import sys
import time
from datetime import datetime

from bot.bootstrap.client import app
from bot.lifecycle.channel_check import check_all_channels
from bot.monitor.state import start_monitor_immediately
from bot.utils import database as db
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)

# P5-42: a BANNED/revoked bot token used to be logged as a warning and the
# bot kept "running" while every send failed forever - no alert, no marker,
# nothing. This module writes a marker file the supervisor (start.sh) can
# read and exits with a distinctive code so the operator/monitoring notices.
_BAN_MARKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "BOT_TOKEN_BANNED.marker"
)

# RPC error names that mean the TOKEN (not the network) is dead: retrying is
# futile. Everything else (network/timeout) keeps normal retry behaviour.
_TOKEN_FATAL_MARKERS = (
    "AUTH_TOKEN_INVALID",
    "AUTH_TOKEN_EXPIRED",
    "AUTH_TOKEN_DUPLICATE",
    "USER_DEACTIVATED_BAN",
    "USER_DEACTIVATED",
)


def _write_ban_marker(reason: str) -> None:
    try:
        os.makedirs(os.path.dirname(_BAN_MARKER_PATH), exist_ok=True)
        with open(_BAN_MARKER_PATH, "w", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat()}\n{reason}\n")
    except Exception:
        logger.debug("ban marker write failed", exc_info=True)


def check_ban_marker() -> bool:
    """True when a previous run flagged the token as banned. The supervisor
    (start.sh) uses this to back off instead of crash-looping."""
    return os.path.exists(_BAN_MARKER_PATH)

# Fire-and-forget startup tasks are scheduled through spawn_background(), which
# keeps a STRONG reference to each task. asyncio only holds a weak one, so a
# bare ``asyncio.create_task(...)`` whose return value is discarded can be
# garbage-collected mid-await and silently cancelled - for the pending-session
# recovery that means an entire backlog of paid-for accounts is never reconciled
# after a restart, with no error anywhere.


async def _run_backfill():
    """Translate existing admin-custom templates (background, idempotent)."""
    from bot.services.translator import backfill_existing_templates
    report = await asyncio.to_thread(backfill_existing_templates)
    print(f"✅ AUTO-TRANSLATE BACKFILL: {report.get('translated', 0)}/{report.get('processed', 0)} "
          f"templates translated ({len(report.get('failed', []))} failed)")


async def startup():
    """Startup tasks after bot connects."""
    try:
        db_initialized = await db.init_database()
        if db_initialized:
            print("✅ Database initialized for audit logging")
        else:
            logger.warning("⚠️ Database not available - using JSON storage only")
    except Exception as e:
        logger.warning(f"⚠️ Database init error (non-critical): {e}")

    try:
        me = await app.get_me()
        print(f"✅ Bot connected as: @{me.username} (ID: {me.id})")
        # The bot answered: any previous ban marker is stale (token restored
        # or the ban lifted) - clear it so the supervisor resumes normally.
        if check_ban_marker():
            try:
                os.remove(_BAN_MARKER_PATH)
                logger.info("Bot token works again - ban marker removed")
            except OSError:
                pass
    except Exception as e:
        error_str = str(e).upper()
        if any(marker in error_str for marker in _TOKEN_FATAL_MARKERS):
            _write_ban_marker(str(e))
            logger.critical(
                f"🚨 BOT TOKEN IS DEAD ({e}) - marker written to {_BAN_MARKER_PATH}. "
                f"Exiting (code 2) so the supervisor/alerting can react. "
                f"Replace BOT_TOKEN in .env to continue."
            )
            print(f"🚨 FATAL: Bot token banned/invalid - exiting. Reason: {e}")
            sys.exit(2)
        logger.warning(f"⚠️ Could not get bot info (transient?): {e}")

    accessible, not_accessible = await check_all_channels()

    # Pending-session recovery runs in the BACKGROUND (no hard time limit) so
    # a large backlog is drained as fast as the API pool allows instead of
    # being abandoned after 10 minutes. The monitor is started immediately
    # afterwards, so sessions whose deadline passed while the bot was down are
    # re-processed promptly by the first monitor cycle (stale claims are
    # auto-released after STALE_CLAIM_MAX_AGE_SECONDS).
    try:
        from bot.utils.pending_recovery import run_pending_recovery, finalize_expired_sessions

        async def _run_background_recovery():
            try:
                print("🔄 Starting PENDING SESSION RECOVERY...")
                recovery_stats = await run_pending_recovery(app, max_time_minutes=None)
                if recovery_stats["total"] > 0:
                    print(f"✅ RECOVERY COMPLETED: {recovery_stats['success']} success, "
                          f"{recovery_stats['rejected']} rejected, {recovery_stats['multi_device']} multi-device")
                else:
                    print("✅ No pending sessions needed recovery")

                print("🧹 Starting EXPIRED SESSION CLEANUP...")
                expired_stats = await finalize_expired_sessions(app)
                if expired_stats.get("deleted_no_otp", 0) > 0 or expired_stats.get("expired_old", 0) > 0:
                    print(f"✅ EXPIRED CLEANUP: {expired_stats.get('deleted_no_otp', 0)} no-OTP, "
                          f"{expired_stats.get('expired_old', 0)} old sessions finalized")

                print("🔁 Starting ORPHANED SESSION RECONCILIATION...")
                from bot.recovery.reconcile import reconcile_orphaned_sessions
                reconcile_stats = await reconcile_orphaned_sessions(app)
                if reconcile_stats["credited"] > 0 or reconcile_stats["rejected"] > 0:
                    print(f"✅ RECONCILIATION: {reconcile_stats['credited']} balance credited, "
                          f"{reconcile_stats['rejected']} rejected, "
                          f"{reconcile_stats['message_sent']} messages delivered")
            except Exception as recovery_err:
                logger.warning(f"⚠️ Pending recovery error (non-critical): {recovery_err}")

        spawn_background(_run_background_recovery(), "pending-session-recovery")
        print("🔄 Background pending-session recovery scheduled (no 10-min cap)")
    except Exception as recovery_err:
        logger.warning(f"⚠️ Pending recovery scheduling error (non-critical): {recovery_err}")

    start_monitor_immediately()

    # P5-49 (self-audit fix): the in-loop ensure_monitor_alive() call can never
    # respawn a DEAD monitor task (a dead task cannot run its own loop). This
    # independent watchdog runs OUTSIDE the monitor and re-arms it.
    async def _monitor_external_watchdog():
        from bot.monitor.state import ensure_monitor_alive
        while True:
            try:
                await asyncio.sleep(120)
                await ensure_monitor_alive()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.warning(f"monitor external watchdog error: {err}")

    try:
        spawn_background(_monitor_external_watchdog(), "monitor-external-watchdog")
    except Exception as watchdog_err:
        logger.warning(f"⚠️ monitor watchdog startup error (non-critical): {watchdog_err}")

    # P4-33: background proxy watchdog - scheduled probing of every enabled
    # proxy, results feed the health stats/breaker, dead proxies raise ONE
    # rate-limited admin alert and the last snapshot is shown in the panel.
    try:
        from bot.monitor.proxy_watchdog import start_proxy_watchdog
        start_proxy_watchdog()
    except Exception as watchdog_err:
        logger.warning(f"⚠️ Proxy watchdog startup error (non-critical): {watchdog_err}")

    # Backfill: translate any admin-custom English template that predates the
    # auto-translate feature into every supported language. Idempotent - after
    # the first run the per-language cache exists, so later boots make zero
    # network calls. Runs in the background so it never blocks startup.
    try:
        spawn_background(_run_backfill(), "auto-translate-backfill")
        print("🌐 Auto-translate backfill scheduled for existing custom templates")
    except Exception as backfill_err:
        logger.warning(f"⚠️ Auto-translate backfill setup error (non-critical): {backfill_err}")

    print("\n" + "="*50)
    print("✅ Bot is now running! Monitor started automatically.")
    if not_accessible > 0:
        logger.warning(f"⚠️ Warning: {not_accessible} channel(s) not accessible - reports may fail!")
    print("="*50 + "\n")
