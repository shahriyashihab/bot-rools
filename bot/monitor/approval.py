"""Account status check + approve + credit flow (full validation)."""
import logging
from typing import Any, Dict, Tuple

from bot.services.accounting import credit_and_log_session
from bot.utils.session_handler import check_account_status, get_country_rate
from bot.utils.storage import approve_pending_session, get_pending_session

logger = logging.getLogger(__name__)


async def run_account_status_and_approve(
    session_id: str,
    session_string: str,
    phone: str,
    user_id: str,
    country_code: str,
    rate: float
) -> Tuple[bool, float, str, Any, Dict[str, Any]]:
    """Check account status, update the session rate and approve + credit.

    Returns ``(success, rate, account_status, status_details, credit_result)``.
    ``rate`` is replaced by ``actual_rate`` derived from account status.
    """
    # CHECK ACCOUNT STATUS BEFORE APPROVING
    print(f"🔍 Checking account status for {phone}...")
    try:
        _row = get_pending_session(session_id) or {}
        from bot.utils.proxy_connector import get_pinned_proxy_url
        _pin = get_pinned_proxy_url(_row, _row.get("session_data", {}))
    except Exception:
        _pin = None
    account_status, status_details = await check_account_status(
        session_string,
        country_code=country_code,
        preferred_proxy_url=_pin,
    )
    print(f"📊 Account Status: {account_status}")
    print(f"📋 Status Details: {status_details}")

    # P4-32: an INCONCLUSIVE check (connect failure, bot-side flood) must not
    # silently DOWNGRADE the payout. The stored rate (set after the delayed
    # task's successful check) is the best-known verdict; a downgraded rate
    # from an inconclusive check is replaced by the incoming one. A genuinely
    # better/worse definitive status still updates the rate.
    is_inconclusive = isinstance(status_details, dict) and (
        status_details.get("inconclusive") or status_details.get("check_method") == "connect_error"
    )
    if is_inconclusive and rate > 0:
        print(f"🛡️ Inconclusive status check for {phone} - keeping stored rate ${rate} "
              f"(no downgrade from inconclusive verdict)")
        actual_rate = rate
        account_status = account_status or "spam_free"
    else:
        # Get correct rate based on account status
        actual_rate = get_country_rate(country_code, account_status, user_id=user_id)
        print(f"💰 Rate for {account_status}: ${actual_rate} (Original rate was ${rate})")
        # CORE CONFIG: "Accept Restricted" OFF for this country refuses the
        # payout for a limited account (status unchanged, rate forced to 0 so
        # the credit is 0 and the pricing math stays untouched).
        try:
            from bot.utils.storage import get_country_setting_bool as _cc_bool
            if (
                account_status == "limit"
                and not _cc_bool(country_code, "accept_restricted")
            ):
                print(f"⛔ ACCEPT RESTRICTED OFF for {country_code}: limited account {phone} credited at 0")
                actual_rate = 0.0
        except Exception:
            pass

    # Update the session data with correct rate and status BEFORE calling approve_pending_session
    from bot.utils.storage import set_pending_session_field
    set_pending_session_field(session_id, "rate", actual_rate)
    set_pending_session_field(session_id, "account_status", account_status)
    set_pending_session_field(session_id, "status_details", status_details)

    # Use actual_rate for the rest of the flow
    rate = actual_rate

    # CREDIT FIRST, APPROVE SECOND.
    #
    # ``atomic_credit_balance_for_session`` already sets ``status="approved"``
    # and ``balance_credited=True`` inside ONE lock, so the credit itself is the
    # approval. The old order (approve -> credit) could flip the row to
    # ``approved`` and then fail to credit; every repair scanner filters on
    # ``status == "pending"``, so such a row became invisible to all of them and
    # the user was never paid - permanently, with no error anywhere.
    print(f"✅ Session {session_id} validated successfully - Crediting with {account_status} rate...")

    # Transaction log for the full-validation path is written by the user
    # notification flow (services.notifications), so accounting only credits
    # balance + analytics here (include_log=False).
    credit_result = await credit_and_log_session(
        session_id=session_id,
        user_id=int(user_id),
        country_code=country_code,
        rate=rate,
        include_log=False,
    )

    if credit_result:
        success, msg = True, f"Session {session_id} credited and approved"
    else:
        # Credit refused: either it was already credited by another path
        # (duplicate - harmless, the session is settled) or the session row is
        # gone / rejected / expired. Only the "already settled" case may report
        # success, so a genuinely unsettled session is never announced as paid.
        session_row = get_pending_session(session_id) or {}
        already_settled = bool(session_row.get("balance_credited")) or \
            session_row.get("status") == "approved"
        if already_settled:
            success, msg = True, f"Session {session_id} was already credited (duplicate skipped)"
        else:
            success, msg = False, f"Session {session_id} could not be credited"
            logger.error(
                f"❌ CREDIT FAILED for {phone} (session {session_id}) - "
                f"row left in status='{session_row.get('status', 'missing')}' so the "
                f"reconciler can retry it. NOT approving."
            )

    print(f"💾 Credit/approval result: {msg}")

    if success:
        # Idempotent belt-and-braces: the credit already flipped the status, but
        # if a duplicate credit was skipped the row may still say "pending".
        if (get_pending_session(session_id) or {}).get("status") != "approved":
            approve_pending_session(session_id)

    return success, rate, account_status, status_details, credit_result
