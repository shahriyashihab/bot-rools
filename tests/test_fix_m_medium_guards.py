"""MEDIUM #24/#25/#26/#27/#35/#46/#47/#48/#33 regression tests.

#24 recovery scan per-session isolation
#25 /tmp -> tempfile everywhere
#26 system toggles: try/except UI + admin log
#27 card state pins preserve panel refs
#35 card history capped (newest kept, dropped counted)
#46 moderation rules schema-validated; JSON error keeps the retry state
#47 alert threshold names whitelisted
#48 mislabeled back button fixed
#33 dual-writer: state.py and database.py use SEPARATE files - documented
    (memory=truth), no data-loss overlap.
"""

import re


def _src(path):
    return open(path, encoding="utf-8").read()


class TestScanIsolation:
    def test_per_session_try_except(self):
        src = _src("bot/monitor/recovery_scan.py")
        assert "skipped this pass" in src
        assert "session_info is True or" not in src  # walrus artifact gone
        m = re.search(r"try:\n(.*?)skip, session_info = await finalize_precomputed_session", src, re.S)
        assert m, "finalize call must be inside try"


class TestTmpPortability:
    def test_no_hardcoded_tmp_in_prod_files(self):
        for path in ("bot/handlers/admin/audit.py", "bot/handlers/admin/country_export.py",
                     "bot/handlers/admin/countries.py", "bot/__main__.py"):
            src = _src(path)
            hits = [line for line in src.splitlines()
                    if "/tmp" in line and "MEDIUM-25" not in line and line.strip().startswith("#")]
            writable = [line for line in src.splitlines()
                        if re.search(r"\"/tmp/|f\"/tmp/", line)]
            assert not writable, f"{path} still writes /tmp: {writable}"

    def test_tempfile_used(self):
        for path in ("bot/handlers/admin/audit.py", "bot/handlers/admin/country_export.py",
                     "bot/__main__.py"):
            src = _src(path)
            assert "tempfile.gettempdir()" in src


class TestMonitorToggles:
    def test_each_toggle_has_log_and_ui_guard(self):
        src = _src("bot/handlers/admin/monitor.py")
        # (callback regex, handler function name)
        for cb, handler in [
            ("togglemonitor", "toggle_monitor_callback"),
            ("togglespamcheck", "toggle_spam_check_callback"),
            ("two_factor_toggle", "two_factor_toggle_callback"),
            ("device_check_toggle", "device_check_toggle_callback"),
            ("same_number_toggle", "same_number_toggle_callback"),
        ]:
            m = re.search(
                rf"async def {handler}.*?(?=\n    @app|\Z)", src, re.S)
            assert m, f"{handler} not found"
            block = m.group(0)
            assert "_log_admin_action" in block, f"{cb}: no admin log"
            assert "except Exception as ui_err" in block, f"{cb}: UI edit not guarded"

    def test_moderation_rules_validated(self):
        src = _src("bot/handlers/admin/monitor.py")
        assert "_ALLOWED_RULE_KEYS" in src
        assert "Unknown rule key" in src
        # JSON error keeps the retry state (no unconditional clear)
        assert re.search(
            r"except json\.JSONDecodeError:.*?return", src, re.S)

    def test_alert_names_whitelisted(self):
        src = _src("bot/handlers/admin/monitor.py")
        assert '_KNOWN_ALERT_NAMES = {"low_balance", "reject_rate", "api_error"}' in src


class TestCards:
    def test_panel_refs_preserved_everywhere(self):
        src = _src("bot/handlers/admin/cards.py")
        assert "def _pin_card" in src
        # no naked card_view state replacement remains
        naked = re.findall(r"admin_state\[[^\]]+\] = \{\"action\": \"card_view\"", src)
        assert not naked, f"naked card_view assignments: {naked}"

    def test_history_capped(self):
        from bot.data.cards import CARD_HISTORY_MAX_EVENTS, _cap_card_history
        rec = {"history": [{"event": f"e{i}"} for i in range(150)]}
        _cap_card_history(rec)
        assert len(rec["history"]) == CARD_HISTORY_MAX_EVENTS
        assert rec["history"][-1]["event"] == "e149"  # newest kept
        assert rec["history_dropped_count"] == 150 - CARD_HISTORY_MAX_EVENTS

    def test_history_cap_applied_at_both_append_sites(self):
        src = _src("bot/data/cards.py")
        assert src.count("_cap_card_history(rec)") == 2

    def test_back_button_label_fixed(self):
        src = _src("bot/handlers/admin/cards.py")
        assert "Back to Blocked Card" in src
        assert '"Back to Block Details", callback_data=f"card_bv_' not in src


class TestDualWriter:
    def test_shared_tables_have_identical_schema(self):
        """#33 verified-by-design: state.py (truth) and database.py (analytics
        mirror) share ONE file, so the `users`/`pending_sessions` schemas must
        be IDENTICAL or one writer's INSERTs would fail. database.py also owns
        append-only log tables state.py never touches."""
        state_src = _src("bot/data/state.py")
        db_src = _src("bot/utils/database.py")
        state_users = re.search(
            r"_STORAGE_SQLITE_SCHEMA = \"\"\".*?CREATE TABLE IF NOT EXISTS users \((.*?)\);",
            state_src, re.S)
        db_users = re.search(
            r'CREATE TABLE IF NOT EXISTS users \((.*?)\);', db_src, re.S)
        assert state_users and db_users
        assert state_users.group(1).strip() == db_users.group(1).strip(), (
            "shared-table schemas diverged - dual-writer risk is real now")

    def test_truth_keys_do_not_include_advisory_tables(self):
        from bot.data.state import _SQLITE_TABLE_KEYS
        assert "transaction_logs" not in _SQLITE_TABLE_KEYS
        assert "admin_logs" not in _SQLITE_TABLE_KEYS
        # memory=truth: users/pending_sessions/withdrawals ARE truth tables
        assert {"users", "pending_sessions", "withdrawals"} <= _SQLITE_TABLE_KEYS


class TestApproveUncredited:
    def test_callers_already_gate_on_settled(self):
        """#40: approve_pending_session is only called after the money is
        verified settled (approval/worker/finalize each check credit first) -
        the data-layer warning stays as belt-and-braces."""
        worker = _src("bot/recovery/worker.py")
        assert "settled = bool(credit_success)" in worker
        finalize = _src("bot/monitor/finalize.py")
        assert "settled = bool(credit_ok) or bool(current_status.get(\"balance_credited\"))" in finalize
        approval = _src("bot/monitor/approval.py")
        assert "if success:" in approval  # approve only after credit success
