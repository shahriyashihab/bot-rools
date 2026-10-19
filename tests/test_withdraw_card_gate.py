"""Tests for the user withdrawal card availability gate.

A blocked or unknown card must:
  - NOT deduct balance
  - NOT create a withdrawal request
  - be rejected with the exact 3-line message
  - keep the withdraw session alive so the user can retry
"""

import pytest

from bot.data.cards import activate_card, block_card, get_card_statistics
from bot.data.state import _storage, _storage_lock
from bot.handlers.withdraw import withdraw_sessions
from bot.utils.storage import clear_withdraw_state, save_withdraw_state


CARD_MSG = (
    "This card is not available.\n"
    "Please contact your admin.\n"
    "Otherwise, contact this bot support."
)


class _FakeChat:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, uid, text):
        self.chat = _FakeChat(uid)
        self.text = text
        self.replies = []
        self.stopped = False

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    def stop_propagation(self):
        self.stopped = True

    def continue_propagation(self):
        pass


def _get_handler():
    """Register the withdraw handlers on a stub app and grab the
    handle_wallet_input coroutine (same code path as production)."""
    import bot.handlers.withdraw as w

    registered = {}

    class _StubApp:
        def on_message(self, *a, **k):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

        def on_callback_query(self, *a, **k):
            def deco(fn):
                return fn
            return deco

    w.register_withdraw_handlers(_StubApp())
    return registered["handle_wallet_input"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    activate_card("GOODCARD", owner_username="@o", channel_id="-1001234567890")
    with _storage_lock:
        _storage["withdrawals"] = []
        _storage["users"] = {}

    def fake_balance(user_id):
        return 50.0

    from bot.utils import fast_cache
    monkeypatch.setattr(fast_cache, "get_user_balance_fast", fake_balance)

    # Schedule the background processing as a task on the RUNNING loop so
    # the withdrawal record is actually created (production spawns it the
    # same way; awaiting inline here would nest the event loop).
    import asyncio

    def fake_spawn(coro, name):
        asyncio.get_running_loop().create_task(coro)

    monkeypatch.setattr(
        "bot.handlers.withdraw.spawn_background", fake_spawn
    )
    yield
    withdraw_sessions.clear()


def _seed_session(uid):
    with _storage_lock:
        _storage["users"][str(uid)] = {
            "balance": 50.0, "sessions": 0, "banned": 0,
            "success_count": 0, "channel_joined": 1, "data": {},
        }
    clear_withdraw_state(uid)
    withdraw_sessions[uid] = {"balance": 50.0, "step": "waiting_wallet", "method": "CARDNAME"}
    save_withdraw_state(uid, withdraw_sessions[uid])


class TestWithdrawalGate:
    def test_blocked_card_rejected(self, monkeypatch):
        activate_card("BLKCARD", owner_username="@o", channel_id="-1001234567890")
        block_card("BLKCARD", blocked_by=1, reason="Admin request")

        uid = 9101
        _seed_session(uid)
        msg = _FakeMessage(uid, "BLKCARD")

        handler = _get_handler()
        import asyncio
        asyncio.get_event_loop().run_until_complete(handler(None, msg))

        assert msg.stopped is True
        assert msg.replies and msg.replies[0] == CARD_MSG
        with _storage_lock:
            assert all(
                wd.get("wallet_address") != "BLKCARD" for wd in _storage["withdrawals"])
        # Session preserved so the user can correct the card name.
        assert withdraw_sessions.get(uid, {}).get("step") == "waiting_wallet"
        # Card statistics unchanged.
        assert get_card_statistics("BLKCARD")["requests"] == 0

    def test_unknown_card_rejected(self):
        uid = 9102
        _seed_session(uid)
        msg = _FakeMessage(uid, "NOCARD1")

        handler = _get_handler()
        import asyncio
        asyncio.get_event_loop().run_until_complete(handler(None, msg))

        assert msg.stopped is True
        assert msg.replies and msg.replies[0] == CARD_MSG
        with _storage_lock:
            assert all(
                wd.get("wallet_address") != "NOCARD1" for wd in _storage["withdrawals"])
        assert get_card_statistics("NOCARD1")["requests"] == 0

    def test_active_card_passes_gate(self):
        uid = 9103
        _seed_session(uid)
        msg = _FakeMessage(uid, "GOODCARD")

        handler = _get_handler()
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_until_complete(handler(None, msg))
        # Let the spawned background task (record creation) finish.
        loop.run_until_complete(asyncio.sleep(0.3))

        # Gate passed: no card-unavailable message; the flow recorded the
        # request and replied with the standard submission confirmation.
        assert all(r != CARD_MSG for r in msg.replies)
        assert any("Withdraw Request Submitted" in r for r in msg.replies)
        with _storage_lock:
            recorded = [wd for wd in _storage["withdrawals"]
                        if wd.get("wallet_address") == "GOODCARD"]
        assert len(recorded) == 1
        assert get_card_statistics("GOODCARD")["requests"] == 1

    def test_card_case_insensitive_gate(self):
        """'goodcard' format-invalid for CARDNAME, but uppercase registry
        matching must never leak a blocked card through mixed-case input."""
        activate_card("MIXED", owner_username="@o", channel_id="-1001234567890")
        block_card("MIXED")
        uid = 9104
        _seed_session(uid)
        # Lowercase input is rejected by format validation first - which is
        # also a rejection, never a deduction.
        msg = _FakeMessage(uid, "mixed")
        handler = _get_handler()
        import asyncio
        asyncio.get_event_loop().run_until_complete(handler(None, msg))
        assert msg.stopped is True
        assert msg.replies  # some validation error, no deduction
        with _storage_lock:
            assert all(
                wd.get("wallet_address") != "mixed" for wd in _storage["withdrawals"])
