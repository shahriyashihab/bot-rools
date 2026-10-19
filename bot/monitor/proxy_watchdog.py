"""Background proxy watchdog: scheduled probing + admin alerts.

The admin probe buttons are manual only, so between admin visits a dead proxy
was discovered through users' failed connects - silent degradation. This
watchdog (P4-33) closes that gap:

1. SCHEDULED PROBE: every PROXY_WATCHDOG_INTERVAL seconds every ENABLED pool
   proxy (of enabled pools) plus the global default is probed with the real
   protocol handshake (proxy_probe). Results are recorded through
   ``record_proxy_attempt`` so they feed the SAME health stats and circuit
   breaker the live traffic uses (P4-34).
2. SNAPSHOT: the last pass's outcome is persisted under
   ``_storage["proxy"]["watchdog"]`` so the admin panel can show an
   aggregated dead-count without running a probe itself.
3. CONFIRMED-DEATH ALERTING (P4-45): a single failed pass NEVER alerts. A
   proxy is "confirmed dead" only after PROXY_WATCHDOG_CONFIRM_PASSES
   (default 2) consecutive failed passes, and each probe gets a generous
   ceiling (PROXY_WATCHDOG_PROBE_TIMEOUT, default 15s - the connector's
   first-candidate budget). A slow-but-alive proxy that times out one pass
   (busy fleet, provider hiccup, event-loop stall) therefore never pages the
   admin; only a proxy that stays dead - genuinely unusable - does. The
   streak and confirmed state are persisted, so a restart does not reset
   confirmation memory.
4. ADMIN ALERT: on a confirmed healthy->dead transition (and when a
   country's whole pool dies) the admin gets ONE Telegram message,
   rate-limited by PROXY_WATCHDOG_ALERT_COOLDOWN_SECONDS. Dead->alive
   recovery also alerts - and immediately, good news is never delayed - so
   the admin knows the fleet healed.

The watchdog never raises into its loop: probing is fire-and-forget and a
probe failure is data, not a crash. It starts with the bot (lifecycle/startup)
and stops with the event loop.
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Probe cadence (env-tunable). 300s keeps the per-pass work trivial (a batch
# of 15 probes at ≤15s each) and catches a dead proxy within minutes without
# adding meaningful load.
PROXY_WATCHDOG_INTERVAL_SECONDS = int(os.getenv("PROXY_WATCHDOG_INTERVAL", "300") or 300)
# Delay before the FIRST pass (short so the admin learns about an already-dead
# fleet at startup, not one interval later). Module constant so tests can
# shorten it without patching asyncio internals; production default unchanged.
PROXY_WATCHDOG_FIRST_PASS_DELAY_SECONDS = int(
    os.getenv("PROXY_WATCHDOG_FIRST_PASS_DELAY", "30") or 30
)
# Minimum delay between two ADMIN ALERTS (never spam the admin chat).
PROXY_WATCHDOG_ALERT_COOLDOWN_SECONDS = int(
    os.getenv("PROXY_WATCHDOG_ALERT_COOLDOWN", "600") or 600
)
# P4-45: per-probe ceiling. The watchdog handshake (TCP + auth + tunnel to a
# Telegram DC) gets a generous budget so a healthy-but-busy proxy is not
# mislabeled dead: 15s matches the connector's first-candidate connect budget
# (proxy_connector._FIRST_CANDIDATE_CONNECT_TIMEOUT) and is 2x the old 8s
# value that produced transient false alerts on slow passes.
PROXY_WATCHDOG_PROBE_TIMEOUT = float(
    os.getenv("PROXY_WATCHDOG_PROBE_TIMEOUT", "15") or 15
)
# P4-45: consecutive failed passes required before a proxy is CONFIRMED dead
# and an alert is sent. One failed pass only arms the streak (snapshot data);
# the second consecutive failure pages the admin. A single flaky pass never
# alerts. Recovery alerts fire immediately regardless of this threshold.
PROXY_WATCHDOG_CONFIRM_PASSES = max(
    1, int(os.getenv("PROXY_WATCHDOG_CONFIRM_PASSES", "2") or 2)
)
# Alert immediately when this many ENABLED probes of one country are dead.
PROXY_WATCHDOG_COUNTRY_DEAD_ALERT = 1
# P4-43: probes never run unbounded in parallel - one batch of this many
# targets at a time (same effect as a semaphore, without event-loop binding).
PROXY_WATCHDOG_PROBE_BATCH_SIZE = 15
# P4-39/44: while the all-dead (or per-country) state PERSISTS, re-alert with
# a "STILL DOWN" prefix at most once every 30 minutes per country (in-memory
# general alert cooldown stays 10 min; the persistent per-country timer is
# separate and survives restarts).
PROXY_WATCHDOG_STILL_DOWN_ALERT_SECONDS = int(
    os.getenv("PROXY_WATCHDOG_STILL_DOWN_ALERT", "1800") or 1800
)

_started = False
_last_alert_ts = 0.0


def get_proxy_watchdog_snapshot() -> Dict:
    """Last watchdog pass, persisted for the admin panel (dead counts)."""
    try:
        from bot.data.state import _storage
        snap = (_storage.get("proxy") or {}).get("watchdog")
        return snap if isinstance(snap, dict) else {}
    except Exception:
        return {}


def _persist_snapshot(snapshot: Dict) -> None:
    try:
        from bot.data.state import _storage
        from bot.data.proxies import _PROXY_BLOB_LOCK, _persist_health_debounced
        with _PROXY_BLOB_LOCK:
            root = _storage.get("proxy")
            if not isinstance(root, dict):
                root = {}
                _storage["proxy"] = root
            root["watchdog"] = snapshot
        # S1: row-level settings persistence - the health-debounced persist
        # below only arms a save; the settings-key mark tells the sync the
        # proxy blob (with this snapshot) actually changed.
        try:
            from bot.data.state import _mark_settings_key_dirty
            _mark_settings_key_dirty("proxy")
        except ImportError:
            pass
        _persist_health_debounced()
    except Exception:
        logger.debug("proxy watchdog: snapshot persist failed", exc_info=True)


async def _alert_admin(text: str) -> None:
    """Send one alert to the admin. Rate-limited; never raises."""
    global _last_alert_ts
    now = time.monotonic()
    if now - _last_alert_ts < PROXY_WATCHDOG_ALERT_COOLDOWN_SECONDS:
        return
    _last_alert_ts = now
    try:
        from bot.config import ADMIN_ID
        from bot.bootstrap.client import app
        await app.send_message(int(ADMIN_ID), text)
        logger.info("proxy watchdog: alert sent to admin")
    except Exception as err:
        logger.debug(f"proxy watchdog: alert delivery failed: {err}")


async def _probe_pass() -> Optional[Dict]:
    """One full probe pass over all enabled proxies. Returns the snapshot."""
    # NOTE: every name used below MUST be imported here (or at module level).
    # ``mask_proxy_url`` was once missing from this block: the first pass that
    # discovered a dead proxy raised NameError inside the result loop, the
    # loop-level handler swallowed it, and the watchdog silently lost dead
    # detection, alerts, the persisted snapshot and the all-dead gate - while
    # appearing to work on an all-healthy fleet. Do not remove it.
    from bot.data.proxies import (
        get_country_proxy_config,
        get_default_proxy,
        get_proxy_countries,
        mask_proxy_url,
        parse_proxy_url,
        record_proxy_attempt,
    )
    from bot.utils.proxy_probe import probe_proxy

    targets: List[Dict] = []  # {cc, url, parsed, is_default}
    for cc in get_proxy_countries():
        cfg = get_country_proxy_config(cc)
        if not cfg or not cfg.get("enabled"):
            continue
        for entry in cfg["proxies"]:
            if not entry.get("enabled"):
                continue
            ok, parsed, _ = parse_proxy_url(entry.get("url", ""))
            if ok and parsed is not None:
                targets.append({"cc": cc, "url": entry["url"], "parsed": parsed, "is_default": False})
    default_url = get_default_proxy()
    if default_url:
        ok, parsed, _ = parse_proxy_url(default_url)
        if ok and parsed is not None:
            # The default is attributed per-country by the connect plan; probe
            # it once per country that would actually use it (no pool).
            for cc in get_proxy_countries():
                cfg = get_country_proxy_config(cc)
                if not cfg or not any(p.get("enabled") for p in cfg["proxies"]):
                    targets.append({"cc": cc, "url": default_url, "parsed": parsed, "is_default": True})

    if not targets:
        return None

    # P4-43: bound probe concurrency. Probing all targets at once can stack
    # hundreds of sockets through dead proxies; batches of 15 keep the pass
    # parallel-but-bounded (the per-probe budget is untouched).
    all_results: List[tuple] = []
    for start_idx in range(0, len(targets), PROXY_WATCHDOG_PROBE_BATCH_SIZE):
        batch = targets[start_idx:start_idx + PROXY_WATCHDOG_PROBE_BATCH_SIZE]
        batch_results = await asyncio.gather(*[
            _probe_target(t["parsed"]) for t in batch
        ])
        all_results.extend(batch_results)

    dead_by_cc: Dict[str, list] = {}
    alive_by_cc: Dict[str, list] = {}
    for target, (ok, detail) in zip(targets, all_results):
        # Feed the SAME health state the live traffic uses (P4-34), but as a
        # PROBE (P4-43): ok/fail counters + last_ts update and a successful
        # probe resets the consecutive-failure streak, while a probe failure
        # never increments ``consecutive_fail`` - probes alone cannot open the
        # circuit breaker for a healthy-but-slow proxy.
        try:
            record_proxy_attempt(target["cc"], target["parsed"], ok, from_probe=True)
        except Exception:
            pass
        if ok:
            # P4-45: the loop needs to know WHICH proxies passed so it can
            # reset their dead-streaks. Masked keys only.
            alive_by_cc.setdefault(target["cc"], []).append(mask_proxy_url(target["url"]))
        if not ok:
            # B3b FIX: store the MASKED URL, never the raw one. The snapshot
            # is persisted inside ``_storage["proxy"]["watchdog"]`` (and can
            # reach the admin export) - a raw URL here carried the proxy
            # password past every redaction layer.
            dead_by_cc.setdefault(target["cc"], []).append({
                "proxy": mask_proxy_url(target["url"]),
                "detail": str(detail)[:80],
            })

    total = len(targets)
    dead_total = sum(len(v) for v in dead_by_cc.values())
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "dead_count": dead_total,
        "dead_by_cc": dead_by_cc,
        "alive_by_cc": alive_by_cc,
    }


async def _probe_target(parsed) -> tuple:
    from bot.utils.proxy_probe import probe_proxy
    try:
        # P4-45: generous ceiling (default 15s) - matches the connector's
        # first-candidate budget so "probe dead" and "real connect failed"
        # agree even for slow-but-alive proxies.
        return await probe_proxy(parsed, timeout=float(PROXY_WATCHDOG_PROBE_TIMEOUT))
    except Exception as err:  # pragma: no cover - defensive
        return False, f"{type(err).__name__}: {err}"


async def proxy_watchdog_loop() -> None:
    """Long-lived watchdog task. Started once from lifecycle startup."""
    global _started
    if _started:
        return
    _started = True
    print(f"🐕 PROXY WATCHDOG started (probe every {PROXY_WATCHDOG_INTERVAL_SECONDS}s)")
    first_pass = True
    while True:
        try:
            # First pass runs shortly after startup so the admin learns about
            # an already-dead fleet immediately, not one interval later.
            await asyncio.sleep(
                PROXY_WATCHDOG_FIRST_PASS_DELAY_SECONDS if first_pass
                else PROXY_WATCHDOG_INTERVAL_SECONDS
            )
            first_pass = False
            from bot.data.proxies import (
                is_proxy_system_enabled,
                is_proxy_system_down,
                set_proxy_system_down,
            )
            if not is_proxy_system_enabled():
                continue

            snapshot = await _probe_pass()
            if snapshot is None:
                continue

            # P4-39/44/45: the previous pass's CONFIRMED dead state and the
            # per-proxy dead streaks are PERSISTED inside the watchdog
            # snapshot so a restart does not reset confirmation memory;
            # per-country persistence counters and the all-dead gate live
            # there too.
            prev_snap = get_proxy_watchdog_snapshot()
            dead_streak: Dict[str, Dict[str, int]] = {}
            for cc, streak in (prev_snap.get("dead_streak") or {}).items():
                if isinstance(streak, dict):
                    dead_streak[str(cc)] = {
                        str(k): int(v or 0) for k, v in streak.items() if v
                    }
            prev_confirmed: Dict[str, str] = {
                str(cc): str(keys)
                for cc, keys in (prev_snap.get("confirmed_dead_keys") or {}).items()
                if keys
            }
            dead_cc_persist: Dict[str, dict] = dict(prev_snap.get("dead_cc_persist") or {})

            # P4-45 per-proxy dead streak: +1 for every failed pass, cleared
            # the moment a pass probes the proxy alive. A proxy is CONFIRMED
            # dead only after PROXY_WATCHDOG_CONFIRM_PASSES consecutive failed
            # passes - one transient timeout (busy fleet, provider hiccup,
            # event-loop stall) can never raise an alert.
            for cc, entries in (snapshot.get("dead_by_cc") or {}).items():
                cc_streak = dead_streak.setdefault(str(cc), {})
                for entry in entries:
                    key = str(entry.get("proxy", ""))
                    if key:
                        cc_streak[key] = int(cc_streak.get(key, 0) or 0) + 1
            for cc, keys in (snapshot.get("alive_by_cc") or {}).items():
                cc_streak = dead_streak.get(str(cc))
                if cc_streak:
                    for key in keys:
                        cc_streak.pop(str(key), None)
                    if not cc_streak:
                        dead_streak.pop(str(cc), None)

            confirmed_by_cc: Dict[str, List[str]] = {}
            for cc, streak in dead_streak.items():
                keys = sorted(k for k, n in streak.items() if n >= PROXY_WATCHDOG_CONFIRM_PASSES)
                if keys:
                    confirmed_by_cc[cc] = keys
            current_confirmed_keys = {cc: ",".join(keys) for cc, keys in confirmed_by_cc.items()}
            snapshot["dead_streak"] = dead_streak
            snapshot["confirmed_dead_keys"] = dict(current_confirmed_keys)

            dead_total = int(snapshot.get("dead_count", 0))
            total = int(snapshot.get("total", 0))
            confirmed_total = sum(len(keys) for keys in confirmed_by_cc.values())

            # Transition alerts (confirmed state only): newly_dead fires when
            # a country's confirmed set changes (first confirmation, or a
            # change in WHICH proxies are confirmed dead).
            newly_dead = [
                cc for cc, keys in current_confirmed_keys.items()
                if keys and prev_confirmed.get(cc) != keys
            ]
            # Key-level partial recovery: a country may keep other confirmed
            # dead proxies while one heals - surfaced in the alert text.
            recovered_keys_by_cc: Dict[str, List[str]] = {}
            for cc, keys in prev_confirmed.items():
                prev_set = {k for k in keys.split(",") if k}
                now_set = {k for k in (current_confirmed_keys.get(cc) or "").split(",") if k}
                gone = sorted(prev_set - now_set)
                if gone:
                    recovered_keys_by_cc[cc] = gone

            # P4-44a: PROXY_WATCHDOG_COUNTRY_DEAD_ALERT threshold: a country
            # qualifies for the STILL-DOWN escalation only when its confirmed
            # dead count reaches the threshold.
            threshold = max(1, int(PROXY_WATCHDOG_COUNTRY_DEAD_ALERT or 1))
            alert_cc = [
                cc for cc, keys in current_confirmed_keys.items()
                if keys and len(confirmed_by_cc.get(cc) or []) >= threshold
            ]

            def _confirmed_dead_lines(cc: str) -> List[str]:
                """Alert lines for one country's CONFIRMED dead proxies only."""
                conf = set(confirmed_by_cc.get(cc) or [])
                return [
                    f"• `{e.get('proxy', '')}` — {e.get('detail', '')}"
                    for e in (snapshot.get("dead_by_cc") or {}).get(cc, [])[:5]
                    if str(e.get("proxy", "")) in conf
                ]

            # Escalation / STILL-DOWN tracking (P4-39c + P4-44a): a country
            # that stays in the SAME dead state across passes is re-alerted
            # with a "STILL DOWN" prefix, rate-limited per-country by the
            # persistent 30-minute timer (survives restarts).
            now_ts = datetime.now().isoformat(timespec="seconds")
            still_down_alerts = []
            for cc in alert_cc:
                persist = dead_cc_persist.get(cc)
                if not isinstance(persist, dict):
                    persist = {}
                if cc not in current_confirmed_keys:
                    continue
                if prev_confirmed.get(cc) == current_confirmed_keys.get(cc):
                    passes = int(persist.get("passes", 0) or 0) + 1
                    last_alert_ts = persist.get("last_alert_ts") or ""
                    due = True
                    if last_alert_ts:
                        try:
                            due = (datetime.now() - datetime.fromisoformat(str(last_alert_ts))).total_seconds() >= PROXY_WATCHDOG_STILL_DOWN_ALERT_SECONDS
                        except (TypeError, ValueError):
                            due = True
                    if passes >= 2 and due:
                        still_down_alerts.append(cc)
                        persist["last_alert_ts"] = now_ts
                    persist["passes"] = passes
                    dead_cc_persist[cc] = persist
                else:
                    dead_cc_persist.pop(cc, None)

            # P4-39b: persistent all-dead gate. When EVERY enabled proxy is
            # dead for 2 consecutive passes, new phone submissions are
            # rejected by the phone gate (proxy_system_down flag). Cleared as
            # soon as any proxy probes alive.
            system_was_down = is_proxy_system_down()
            all_dead = bool(total > 0 and dead_total == total)
            if all_dead:
                gate_passes = int(prev_snap.get("system_down_passes", 0) or 0) + 1
                snapshot["system_down_passes"] = gate_passes
                if gate_passes >= 2 and not system_was_down:
                    set_proxy_system_down(True)
                    system_was_down = True
                    print(f"🚨 PROXY WATCHDOG: ALL {total} proxies dead for 2 passes - "
                          "new phone submissions rejected until recovery")
            else:
                snapshot["system_down_passes"] = 0
                if system_was_down:
                    set_proxy_system_down(False)
                    print(f"♻️ PROXY WATCHDOG: proxy system recovered - new phone submissions allowed")

            if still_down_alerts:
                lines = [
                    "🚨🚨 **PROXY ALERT** - STILL DOWN (watchdog)",
                    f"💀 Still dead: **{confirmed_total}/{total}** enabled proxies "
                    f"(confirmed across {PROXY_WATCHDOG_CONFIRM_PASSES}+ passes)",
                    f"🕒 {snapshot.get('ts')}",
                    "",
                ]
                for cc in still_down_alerts:
                    lines.append(f"**{cc}**:")
                    lines.extend(_confirmed_dead_lines(cc))
                await _alert_admin("\n".join(lines))

            # P4-45: only CONFIRMED deaths alert. One message covers both
            # directions - newly confirmed dead proxies AND proxies that
            # recovered since the last pass (recovery is always reported
            # immediately; good news is never delayed).
            alert_countries = sorted(set(newly_dead) | set(recovered_keys_by_cc))
            if alert_countries:
                lines = [
                    "🚨 **PROXY ALERT** (watchdog)",
                    f"💀 Confirmed dead: **{confirmed_total}/{total}** enabled proxies "
                    f"(failed {PROXY_WATCHDOG_CONFIRM_PASSES} consecutive passes)",
                    f"🕒 {snapshot.get('ts')}",
                    "",
                ]
                for cc in alert_countries:
                    if cc in newly_dead:
                        lines.append(f"**{cc}**:")
                        lines.extend(_confirmed_dead_lines(cc))
                    gone = recovered_keys_by_cc.get(cc)
                    if gone:
                        lines.append(f"♻️ recovered: {', '.join('`' + g + '`' for g in gone[:3])}")
                await _alert_admin("\n".join(lines))

            snapshot["dead_cc_persist"] = dead_cc_persist
            # Per-pass transient data: alive lists were only needed for the
            # streak bookkeeping above - do not persist them.
            snapshot.pop("alive_by_cc", None)
            _persist_snapshot(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning(f"proxy watchdog pass failed: {err}")
            await asyncio.sleep(30)


def start_proxy_watchdog() -> None:
    """Spawn the watchdog from lifecycle startup (idempotent)."""
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("proxy watchdog: no running loop at startup - skipped")
            return
        from bot.utils.bg_tasks import spawn_background
        spawn_background(proxy_watchdog_loop(), "proxy-watchdog")
    except Exception as err:
        logger.warning(f"proxy watchdog could not start: {err}")
