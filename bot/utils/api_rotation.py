"""
TRUE ROUND-ROBIN API Rotation System v3.1
==========================================
Every verification uses the NEXT API in sequence:
API 1 → API 2 → API 3 → API 4 → API 5 → API 1 → ...

This ensures:
- Even load distribution across ALL APIs
- Minimum freeze rate (each API gets rest time)
- Maximum daily capacity
- Flood-hit APIs are auto-cooldowned (never picked again until the wait ends)

API LIMITS: There is NO hard daily/hourly session limit. The admin decides
how many accounts each country accepts (country capacity); the bot never
artificially caps an API's throughput. The daily/hourly counters are kept
as informational stats only and never block rotation.
"""

import os
import logging
import threading
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

# Session→API binding table limits. ``session_api_map`` previously grew
# without bound whenever a caller forgot ``release_session_api`` (a hard
# memory leak at 40-50k sessions/day). Stale bindings are swept on every
# assign and the table is hard-capped (oldest bindings evicted first; dicts
# preserve insertion order so "oldest" is exact).
_SESSION_API_MAP_MAX = 5000
_SESSION_API_TTL_SECONDS = 3600.0


@dataclass
class APICredentials:
    """Telegram API credentials with usage tracking"""
    api_id: int
    api_hash: str
    name: str = "default"
    daily_limit: int = 200
    hourly_limit: int = 30
    is_active: bool = True
    
    sessions_today: int = 0
    sessions_this_hour: int = 0
    last_used: Optional[datetime] = None
    last_hour_reset: Optional[datetime] = None
    last_day_reset: Optional[datetime] = None
    
    success_count: int = 0
    fail_count: int = 0
    flood_count: int = 0
    cooldown_until: Optional[datetime] = None
    
    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 1.0
    
    def is_available(self) -> bool:
        """Check if API can be used now.

        Availability is gated ONLY by:
          - whether the API is enabled (is_active)
          - whether it is currently in a flood cooldown

        The daily_limit / hourly_limit fields are informational (shown in the
        admin API monitor) and NEVER block rotation - the admin controls
        country capacity, not API throughput.
        """
        if not self.is_active:
            return False
        if self.cooldown_until and datetime.now() < self.cooldown_until:
            return False

        self._reset_counters()
        return True
    
    def _reset_counters(self):
        """Reset hourly/daily counters if needed"""
        now = datetime.now()
        
        if self.last_hour_reset is None or (now - self.last_hour_reset).total_seconds() >= 3600:
            self.sessions_this_hour = 0
            self.last_hour_reset = now
            
        if self.last_day_reset is None or self.last_day_reset.date() != now.date():
            self.sessions_today = 0
            self.last_day_reset = now
            
    def record_usage(self, success: bool = True, flood: bool = False):
        """Record API usage"""
        self._reset_counters()
        
        self.sessions_today += 1
        self.sessions_this_hour += 1
        self.last_used = datetime.now()
        
        if success:
            self.success_count += 1
        else:
            self.fail_count += 1
            
        if flood:
            self.flood_count += 1
            cooldown_minutes = min(30, 5 * self.flood_count)
            self.cooldown_until = datetime.now() + timedelta(minutes=cooldown_minutes)
            _logger.warning(f"⚠️ API {self.name} flood cooldown: {cooldown_minutes} minutes")


class TrueRoundRobinManager:
    """
    TRUE ROUND-ROBIN API Rotation
    ==============================
    GUARANTEED sequence: API 1 → API 2 → API 3 → API 4 → API 5 → API 1 → ...
    
    Benefits:
    - Each API gets equal load
    - Each API gets 4x rest time between uses (with 5 APIs)
    - Minimum freeze rate
    - Maximum throughput
    - Flood-hit APIs are automatically cooldowned and skipped until the
      wait expires (no API is ever over-used while flooded)
    """
    
    def __init__(self):
        self.apis: List[APICredentials] = []
        self._lock = threading.Lock()
        self._current_index = 0
        self._total_rotations = 0
        self.session_api_map: Dict[str, APICredentials] = {}
        # Parallel assignment timestamps for the stale-binding sweeper.
        self._session_api_ts: Dict[str, datetime] = {}
        
    def add_api(self, api_id: int, api_hash: str, name: str = None,
                daily_limit: int = 200, hourly_limit: int = 30):
        """Add API credentials to rotation pool"""
        if name is None:
            name = f"api_{len(self.apis) + 1}"
            
        api = APICredentials(
            api_id=api_id,
            api_hash=api_hash,
            name=name,
            daily_limit=daily_limit,
            hourly_limit=hourly_limit
        )
        self.apis.append(api)
        _logger.info(f"✅ API added to rotation: {name} (ID: {api_id})")
        
    def add_apis_from_list(self, api_list: List[Tuple[int, str]]):
        """Add multiple APIs from list"""
        for i, (api_id, api_hash) in enumerate(api_list):
            self.add_api(api_id, api_hash, f"api_{i + 1}")
    
    def get_next_api(self) -> Optional[APICredentials]:
        """
        TRUE ROUND-ROBIN: Get the NEXT API in sequence
        Always rotates: 1 → 2 → 3 → 4 → 5 → 1 → 2 → ...
        Skips only if API is in cooldown or disabled
        """
        if not self.apis:
            return None

        with self._lock:
            # Defensive: reset index if the pool shrunk (e.g. runtime removal).
            if self._current_index >= len(self.apis):
                self._current_index = 0

            attempts = 0
            total_apis = len(self.apis)
            
            while attempts < total_apis:
                api = self.apis[self._current_index]
                
                self._current_index = (self._current_index + 1) % total_apis
                self._total_rotations += 1
                
                if api.is_available():
                    _logger.debug(f"🔄 ROUND-ROBIN: Using {api.name} (rotation #{self._total_rotations})")
                    return api
                else:
                    if not api.is_active:
                        reason = "disabled"
                    elif api.cooldown_until:
                        remaining = (api.cooldown_until - datetime.now()).total_seconds()
                        reason = f"cooldown ({remaining:.0f}s left)"
                    else:
                        reason = "unavailable"
                    _logger.debug(f"⏭️ Skipping {api.name} ({reason}), trying next...")
                
                attempts += 1
            
            _logger.warning("⚠️ All APIs unavailable!")
            return None
    
    def _sweep_stale_session_apis_locked(self) -> None:
        """Drop bindings older than the TTL (sessions that crashed or whose
        caller never released). Runs under ``self._lock``. Dropped bindings do
        NOT record usage: a >1h-old binding is a leak artifact, and a late
        record_usage could skew stats or trigger a spurious flood cooldown."""
        now = datetime.now()
        stale = [
            sid for sid, ts in self._session_api_ts.items()
            if (now - ts).total_seconds() > _SESSION_API_TTL_SECONDS
        ]
        for sid in stale:
            self.session_api_map.pop(sid, None)
            self._session_api_ts.pop(sid, None)

    def _enforce_session_api_cap_locked(self) -> None:
        """Hard-cap the binding table (runs under ``self._lock``)."""
        excess = len(self.session_api_map) - _SESSION_API_MAP_MAX
        for _ in range(max(0, excess)):
            oldest = next(iter(self.session_api_map))
            self.session_api_map.pop(oldest, None)
            self._session_api_ts.pop(oldest, None)

    def assign_api_to_session(self, session_id: str) -> Optional[APICredentials]:
        """Assign next API in rotation to a session.

        All mutations of the binding table happen under ``self._lock`` (the
        old lock-free ``+=`` on shared counters could lose updates) and the
        table is bounded (TTL sweep + hard cap applied AFTER the insert), so
        a missing ``release_session_api`` call can no longer leak memory.
        """
        api = self.get_next_api()
        if api:
            with self._lock:
                self._sweep_stale_session_apis_locked()
                self.session_api_map[session_id] = api
                self._session_api_ts[session_id] = datetime.now()
                self._enforce_session_api_cap_locked()
        return api

    def get_session_api(self, session_id: str) -> Optional[APICredentials]:
        """Get API assigned to a session"""
        with self._lock:
            return self.session_api_map.get(session_id)

    def release_session_api(self, session_id: str, success: bool = True, flood: bool = False):
        """Release API from session and record result.

        The map mutation is lock-guarded; ``record_usage`` runs OUTSIDE the
        lock so per-API counter updates never serialize the rotation path.
        """
        with self._lock:
            api = self.session_api_map.pop(session_id, None)
            self._session_api_ts.pop(session_id, None)
        if api:
            api.record_usage(success, flood)
            
    def get_current_api_info(self) -> dict:
        """Get info about current rotation state"""
        if not self.apis:
            return {"status": "no_apis"}
        if self._current_index >= len(self.apis):
            self._current_index = 0
        current_api = self.apis[self._current_index]
        return {
            "current_index": self._current_index,
            "current_api": current_api.name,
            "current_api_id": current_api.api_id,
            "total_rotations": self._total_rotations,
            "next_api": self.apis[(self._current_index + 1) % len(self.apis)].name
        }
            
    def get_stats(self) -> dict:
        """Get API pool statistics"""
        total = len(self.apis)
        available = sum(1 for a in self.apis if a.is_available())
        
        total_today = sum(a.sessions_today for a in self.apis)
        total_success = sum(a.success_count for a in self.apis)
        total_fail = sum(a.fail_count for a in self.apis)
        
        # API limits are NOT enforced (only flood cooldowns are). The
        # "remaining capacity" is informational: None means unlimited.
        explicit_limit = os.getenv("API_DAILY_LIMIT")
        remaining = None
        if explicit_limit:
            remaining = sum(max(0, a.daily_limit - a.sessions_today) for a in self.apis)
        
        return {
            "rotation_mode": "TRUE_ROUND_ROBIN",
            "total_apis": total,
            "available_now": available,
            "current_index": self._current_index,
            "total_rotations": self._total_rotations,
            "sessions_today": total_today,
            "total_success": total_success,
            "total_fail": total_fail,
            "overall_success_rate": total_success / (total_success + total_fail) if (total_success + total_fail) > 0 else 0,
            "remaining_capacity": remaining,
        }
        
    def get_detailed_stats(self) -> List[dict]:
        """Get detailed stats for each API"""
        stats = []
        for i, api in enumerate(self.apis):
            is_current = (i == self._current_index)
            stats.append({
                "name": api.name,
                "api_id": api.api_id,
                "is_current": is_current,
                "is_available": api.is_available(),
                "sessions_today": api.sessions_today,
                "sessions_this_hour": api.sessions_this_hour,
                "daily_limit": api.daily_limit,
                "hourly_limit": api.hourly_limit,
                "success_rate": f"{api.success_rate:.1%}",
                "flood_count": api.flood_count,
                "in_cooldown": api.cooldown_until is not None and datetime.now() < api.cooldown_until,
                "last_used": api.last_used.strftime("%H:%M:%S") if api.last_used else "Never"
            })
        return stats
    
    def get_rotation_visual(self) -> str:
        """Get visual representation of current rotation"""
        if not self.apis:
            return "No APIs configured"
        
        visual = "🔄 API Rotation Status:\n"
        for i, api in enumerate(self.apis):
            marker = "▶️" if i == self._current_index else "  "
            status = "✅" if api.is_available() else "❌"
            visual += f"{marker} {status} {api.name} (today: {api.sessions_today})\n"
        
        visual += f"\n📊 Total rotations: {self._total_rotations}"
        return visual


APIRotationManager = TrueRoundRobinManager

_api_manager: Optional[TrueRoundRobinManager] = None


def get_api_manager() -> TrueRoundRobinManager:
    """Get or create global API manager"""
    global _api_manager
    if _api_manager is None:
        _api_manager = TrueRoundRobinManager()
        _auto_load_extra_apis(_api_manager)
        _auto_load_dynamic_apis(_api_manager)
    return _api_manager


def record_api_usage(api_id=None, api_name=None, success: bool = True, flood: bool = False):
    """Record real usage (and optional flood cooldown) for an API in the pool.

    Looks up the API by ``api_name`` (preferred, unique per pool) or by
    ``api_id``. This is the missing wiring that makes the rotation stats and
    the flood-cooldown actually work: every place that performs a real
    Telegram API call with a rotated API must call this with the outcome.

    - ``success=False`` increments the fail counter.
    - ``flood=True`` increments the flood counter and puts the API in a
      cooldown (5 * flood_count minutes, capped at 30) during which the
      rotation skips it entirely.
    """
    try:
        manager = get_api_manager()
        with manager._lock:
            for api in manager.apis:
                if api_name is not None and api.name == api_name:
                    api.record_usage(success=success, flood=flood)
                    return
            if api_id is not None:
                for api in manager.apis:
                    if int(api.api_id) == int(api_id):
                        api.record_usage(success=success, flood=flood)
                        return
    except Exception as e:
        print(f"⚠️ record_api_usage error: {e}")


def _auto_load_extra_apis(manager: TrueRoundRobinManager):
    """Automatically load extra APIs from config (deduplicated).

    Duplicate api_ids (same ID twice, or an EXTRA API identical to the
    primary API) are skipped so the rotation pool only ever contains UNIQUE
    credentials. The daily/hourly limits are purely informational (they never
    block rotation; see ``APICredentials.is_available``).
    """
    try:
        from bot.config import EXTRA_APIS, API_ID
        if EXTRA_APIS:
            loaded = 0
            seen_ids = {int(API_ID)} | {int(a.api_id) for a in manager.apis}
            for i, (api_id, api_hash) in enumerate(EXTRA_APIS):
                try:
                    api_id_int = int(api_id)
                except (TypeError, ValueError):
                    continue
                if api_id_int in seen_ids:
                    print(f"⏭️ Skipping duplicate EXTRA API {api_id} (already in pool)")
                    continue
                manager.add_api(
                    api_id=api_id_int,
                    api_hash=api_hash,
                    name=f"API_{len(manager.apis) + 1}",
                    daily_limit=int(os.getenv("API_DAILY_LIMIT", "200")),
                    hourly_limit=int(os.getenv("API_HOURLY_LIMIT", "30")),
                )
                seen_ids.add(api_id_int)
                loaded += 1
            print(f"✅ Loaded {loaded} UNIQUE APIs for TRUE ROUND-ROBIN rotation")
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️ Error loading extra APIs: {e}")


def _auto_load_dynamic_apis(manager: TrueRoundRobinManager):
    """Load APIs added at runtime through the admin panel (persisted in storage).

    These survive restarts: on startup they are re-registered from the
    ``extra_apis`` storage key alongside the .env ``EXTRA_API_*`` credentials.
    Duplicate IDs (including the primary API) are skipped.
    """
    try:
        from bot.utils.storage import _storage
        from bot.config import API_ID
        dynamic = _storage.get("extra_apis") or []
        loaded = 0
        seen_ids = {int(API_ID)} | {int(a.api_id) for a in manager.apis}
        for entry in dynamic:
            try:
                api_id = int(entry.get("api_id"))
                api_hash = entry.get("api_hash")
                name = entry.get("name") or f"API_{len(manager.apis) + 1}"
                if api_id and api_hash and api_id not in seen_ids:
                    manager.add_api(api_id, api_hash, str(name))
                    seen_ids.add(api_id)
                    loaded += 1
            except (TypeError, ValueError, AttributeError):
                continue
        if loaded:
            print(f"✅ Loaded {loaded} runtime-added APIs from storage")
    except Exception as e:
        print(f"⚠️ Error loading runtime-added APIs: {e}")


def add_api_runtime(api_id, api_hash, name=None):
    """Add an API to the rotation pool at runtime AND persist it for restarts.

    Returns ``(ok: bool, message: str)``.
    """
    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        return False, "❌ Invalid API ID - must be a number"
    api_hash = str(api_hash or "").strip()
    if not api_hash:
        return False, "❌ API Hash is required"

    try:
        from bot.config import API_ID
        if int(api_id) == int(API_ID):
            return False, f"⚠️ API ID {api_id} is already the PRIMARY API - no need to add it again"
    except Exception:
        pass

    manager = get_api_manager()
    with manager._lock:
        for api in manager.apis:
            if int(api.api_id) == api_id:
                return False, f"⚠️ API ID {api_id} already exists ({api.name})"

        if name is None or not str(name).strip():
            name = f"API_{len(manager.apis) + 1}"
        name = str(name).strip()
        manager.add_api(api_id, api_hash, name)

    # A new API immediately raises the check-phase and live-verdict concurrency
    # caps (no restart needed).
    try:
        from bot.verification.state import refresh_concurrency_limits
        refresh_concurrency_limits()
    except Exception as e:
        _logger.warning(f"⚠️ Could not refresh concurrency limits after API add: {e}")

    # The recovery worker pool scales with the API count too - grow its
    # semaphore so a large pending backlog is drained faster with the new API.
    try:
        from bot.recovery.worker import refresh_recovery_concurrency
        refresh_recovery_concurrency()
    except Exception as e:
        _logger.warning(f"⚠️ Could not refresh recovery concurrency after API add: {e}")

    try:
        from bot.data.state import _mark_settings_key_dirty
        from bot.utils.storage import _storage, save_storage
        extra = _storage.setdefault("extra_apis", [])
        if not any(str(e.get("api_id")) == str(api_id) for e in extra):
            extra.append({"api_id": api_id, "api_hash": api_hash, "name": name})
            # S1: row-level settings persistence for this key only.
            _mark_settings_key_dirty("extra_apis")
            save_storage(immediate=True)
    except Exception as e:
        _logger.warning(f"⚠️ Could not persist added API: {e}")

    return True, f"✅ API added: {name} (ID {api_id})"


def remove_api_runtime(api_id=None, name=None):
    """Remove an API from the rotation pool at runtime AND persist the removal.

    Note: APIs defined in .env (EXTRA_API_*) reappear on the next restart
    unless they are also removed from the .env file. Runtime-added APIs are
    fully removed.

    Returns ``(ok: bool, message: str)``.
    """
    manager = get_api_manager()
    target = None
    for api in manager.apis:
        if api_id is not None:
            try:
                if int(api.api_id) == int(api_id):
                    target = api
                    break
            except (TypeError, ValueError):
                continue
        if name and api.name == name:
            target = api
            break
    if target is None:
        return False, "❌ API not found in the rotation pool"

    with manager._lock:
        if target in manager.apis:
            manager.apis.remove(target)
            # Keep the rotation pointer valid after a runtime removal: if
            # _current_index now points past the end (the removed API was the
            # last one), wrap it so the next get_next_api()/get_stats() calls
            # never hit an IndexError.
            if not manager.apis:
                manager._current_index = 0
            elif manager._current_index >= len(manager.apis):
                manager._current_index = 0
        else:
            return False, "❌ API not found in the rotation pool"

    removed_name = target.name
    removed_id = target.api_id
    is_dynamic = False
    try:
        from bot.data.state import _mark_settings_key_dirty
        from bot.utils.storage import _storage, save_storage
        extra = _storage.get("extra_apis") or []
        before = len(extra)
        _storage["extra_apis"] = [
            e for e in extra if str(e.get("api_id")) != str(removed_id)
        ]
        is_dynamic = len(_storage["extra_apis"]) < before
        if is_dynamic:
            # S1: row-level settings persistence for this key only.
            _mark_settings_key_dirty("extra_apis")
        save_storage(immediate=True)
    except Exception as e:
        _logger.warning(f"⚠️ Could not persist API removal: {e}")

    # The pool shrank: the semaphores are only ever grown, so a removal takes
    # full effect on the next restart (growing is safe, shrinking is not).
    try:
        from bot.verification.state import refresh_concurrency_limits
        refresh_concurrency_limits()
    except Exception as e:
        _logger.warning(f"⚠️ Could not refresh concurrency limits after API removal: {e}")

    msg = f"✅ API removed: {removed_name} (ID {removed_id})"
    if not is_dynamic:
        msg += ("\n⚠️ This API is from `.env` (EXTRA_API_*) — it will return "
                "after a restart unless you also remove it from the `.env` file.")
    return True, msg


def get_all_api_credentials() -> list:
    """Return the full list of API credentials in the pool.

    Each entry is a dict: ``{name, api_id, api_hash, is_dynamic}``. Used by
    the admin panel and by session-format resolution so the real credentials
    that created a session are always reported.
    """
    manager = get_api_manager()
    try:
        from bot.utils.storage import _storage
        dynamic = _storage.get("extra_apis") or []
    except Exception:
        dynamic = []
    dynamic_ids = {str(e.get("api_id")) for e in dynamic}
    result = []
    for api in manager.apis:
        result.append({
            "name": api.name,
            "api_id": api.api_id,
            "api_hash": api.api_hash,
            "is_dynamic": str(api.api_id) in dynamic_ids,
        })
    return result


def init_api_manager(api_list: List[Tuple[int, str]] = None) -> TrueRoundRobinManager:
    """Initialize API manager with credentials"""
    manager = get_api_manager()
    
    if api_list:
        manager.add_apis_from_list(api_list)
        
    return manager


print("🔄 TRUE ROUND-ROBIN API Rotation System v3.0 loaded")
print("   Sequence: API 1 → API 2 → API 3 → API 4 → API 5 → API 1 → ...")
print("   Benefits: Even load, Maximum rest time, Minimum freeze rate, Auto-flood cooldown")
