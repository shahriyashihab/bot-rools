"""C3 + C4 regression tests: pending_sessions blob write discipline.

C3 (audit HIGH #14): field mutators (set/update_pending_session_field) and
the whole-blob replace all mutated the row WITHOUT ``_storage_lock`` - a
concurrent flag write could interleave with a replace and be lost.
All three mutators now share the lock.

C4 (audit HIGH #15): ``add_pending_session`` rebuilt the whole row and
REPLACED the session_data blob - flags written by concurrent paths
(channel_report_pending, reconcile_message_pending, ...) and unknown
top-level keys (finalization_claimed, ...) were silently wiped. The re-add
now MERGES: incoming keys win, unknown keys survive.
"""

import pytest

from bot.data import _mark_dirty, _storage, save_storage_now
from bot.data.sessions import (
    add_pending_session,
    get_pending_session,
    replace_pending_session_data,
    set_pending_session_field,
    update_pending_session_field,
)


def _mk_session_args(session_id: str, session_data: dict):
    return dict(
        session_id=session_id,
        user_id="990051",
        phone="+8801700000000",
        country_code="+880",
        session_data=session_data,
        rate=1.0,
        timeout_seconds=300,
        session_string="str",
        twofa_password="",
        message_id=None,
        phone_message_id=None,
        session_file="",
        telethon_file="",
        spam_status="pending",
        spam_status_details={},
        submitted_at="2026-09-07T10:00:00",
        deadline_at="2026-09-07T10:05:00",
    )


BASE_BLOB = {"ready_to_send": True, "message_type": "SUCCESS"}


@pytest.fixture()
def cleanup_sessions():
    yield
    for sid in ("HC3", "HC4"):
        _storage.get("pending_sessions", {}).pop(sid, None)
    _storage["users"].pop("990051", None)
    save_storage_now()


class TestC3LockDiscipline:
    def test_top_level_field_survives_stale_replace(self, cleanup_sessions):
        add_pending_session(**_mk_session_args("HC3", dict(BASE_BLOB)))
        set_pending_session_field("HC3", "balance_credited", True)
        # a concurrent path re-replaces the blob with a STALE snapshot
        replace_pending_session_data("HC3", dict(BASE_BLOB))
        row = get_pending_session("HC3")
        # balance_credited is a TOP-LEVEL row key - a blob replace must not
        # touch it (and under the lock the two ops are serialized)
        assert row.get("balance_credited") is True

    def test_field_update_after_replace_lands(self, cleanup_sessions):
        add_pending_session(**_mk_session_args("HC3", dict(BASE_BLOB)))
        replace_pending_session_data("HC3", dict(BASE_BLOB))
        update_pending_session_field("HC3", "channel_report_sent", True)
        info = (get_pending_session("HC3") or {}).get("session_data") or {}
        assert info.get("channel_report_sent") is True


class TestC4MergePreserving:
    def test_unknown_session_data_flags_survive_re_add(self, cleanup_sessions):
        add_pending_session(**_mk_session_args("HC4", dict(BASE_BLOB)))
        # concurrent path writes a flag into the persisted blob
        update_pending_session_field("HC4", "channel_report_pending", True)
        # the delayed task then re-adds with a STALE blob (flag absent)
        add_pending_session(**_mk_session_args("HC4", dict(BASE_BLOB)))
        info = (get_pending_session("HC4") or {}).get("session_data") or {}
        assert info.get("channel_report_pending") is True  # C4 wipe GONE
        assert info.get("message_type") == "SUCCESS"  # incoming key still wins

    def test_unknown_top_level_keys_survive_re_add(self, cleanup_sessions):
        add_pending_session(**_mk_session_args("HC4", dict(BASE_BLOB)))
        set_pending_session_field("HC4", "finalization_claimed_by", "monitor")
        add_pending_session(**_mk_session_args("HC4", dict(BASE_BLOB)))
        row = get_pending_session("HC4")
        assert row.get("finalization_claimed_by") == "monitor"

    def test_approved_status_still_preserved(self, cleanup_sessions):
        add_pending_session(**_mk_session_args("HC4", dict(BASE_BLOB)))
        set_pending_session_field("HC4", "status", "approved")
        set_pending_session_field("HC4", "balance_credited", True)
        add_pending_session(**_mk_session_args("HC4", dict(BASE_BLOB)))
        row = get_pending_session("HC4")
        assert row.get("status") == "approved"
        assert row.get("balance_credited") is True
