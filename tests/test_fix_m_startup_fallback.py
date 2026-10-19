"""MEDIUM #22/#23 regression tests: startup load bookkeeping + fallback visibility.

#22: the P7-76 comment claimed the storage load was explicit, but the module
still called bare ``load_storage()`` - leaving ``_storage_initialized`` False
forever so ``ensure_storage_loaded()`` would have re-loaded the DB on the
first guarded reader. The import-time call now routes through ``init_storage``
(idempotent) and the entry points call it explicitly.

#23: a missing SESSION_SECRET silently disabled the crash-backup JSON with
only an error log. The state is now exposed via ``is_fallback_snapshot_enabled``
and shown on the DB Health screen.
"""

import importlib

from bot.data import state as state_mod


class TestStartupBookkeeping:
    def test_initialized_flag_is_true_after_import(self):
        # The import-time init_storage() call must leave the flag set.
        assert state_mod._storage_initialized is True

    def test_init_storage_is_idempotent(self):
        users_before = len(state_mod._storage.get("users", {}))
        state_mod.init_storage()
        state_mod.init_storage()
        assert state_mod._storage_initialized is True
        assert len(state_mod._storage.get("users", {})) == users_before

    def test_ensure_storage_loaded_is_noop_when_initialized(self):
        state_mod.ensure_storage_loaded()
        assert state_mod._storage_initialized is True


class TestFallbackSnapshotVisibility:
    def test_enabled_with_secret_set(self, monkeypatch):
        monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
        monkeypatch.delenv("FALLBACK_ALLOW_PLAINTEXT", raising=False)
        assert state_mod.is_fallback_snapshot_enabled() is True

    def test_disabled_without_any_key(self, monkeypatch):
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("FALLBACK_ALLOW_PLAINTEXT", raising=False)
        assert state_mod.is_fallback_snapshot_enabled() is False

    def test_enabled_with_plaintext_override(self, monkeypatch):
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("FALLBACK_ALLOW_PLAINTEXT", "1")
        assert state_mod.is_fallback_snapshot_enabled() is True

    def test_db_health_screen_surfaces_the_state(self):
        """The DB Health renderer must reference the fallback flag."""
        src = open("bot/handlers/admin/dashboard.py", encoding="utf-8").read()
        assert "is_fallback_snapshot_enabled" in src
        assert "Crash Backup (fallback JSON)" in src
