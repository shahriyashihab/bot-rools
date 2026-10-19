"""
Country rate / timeout lookups for the session verification workflow.
=====================================================================
Wraps storage-backed rate and timeout lookups so verification logic
stays decoupled from the storage implementation.
"""

from bot.utils.storage import (
    get_country_rate as storage_get_country_rate,
    get_country_timeout as storage_get_country_timeout,
    get_user_effective_rate,
    get_country_setting_bool,
)


def get_country_rate(country_code: str, account_status: str = "spam_free", user_id=None) -> float:
    """
    Get verification rate from storage based on account status

    PAYOUT POLICY (unified, C1): the per-country CORE CONFIG switch
    ``accept_restricted`` decides whether a LIMITED account gets paid.
    The policy is applied HERE - the single choke-point every settlement
    path (approval, delayed task, fast path, finalize, reconcile) goes
    through for its rate lookup - so all paths see the SAME truth and the
    payout can no longer depend on which path settled the session.

    Args:
        country_code: Country code like +880
        account_status: One of "spam_free", "spam", "limit"
        user_id: If set, a per-user custom rate (admin "Set Individual User Rate")
                 overrides the country default for this user.

    Returns:
        Rate as float. A rate of 0.0 is a VALID rate (admin set it to 0, i.e.
        free/no payment) and is returned as-is. The 1.00 default is only used
        as a last resort when NO rate could be resolved at all (None).
    """
    if user_id is not None:
        rate = get_user_effective_rate(user_id, country_code, account_status)
    else:
        rate = storage_get_country_rate(country_code, account_status)
    if rate is None:
        rate = 1.00
    if account_status == "limit":
        try:
            if not get_country_setting_bool(country_code, "accept_restricted"):
                # Payout policy: Accept Restricted OFF refuses payment for
                # limited accounts on EVERY settlement path (rate forced to 0;
                # a 0-credit still approves the session without changing the
                # pricing math). Fail-open matches the historical approval.py /
                # worker.py guard semantics (an error never blocks a payout).
                return 0.0
        except Exception:
            pass
    return rate


def get_country_timeout(country_code: str) -> int:
    """
    Get verification timeout from storage (capacity system)
    This reads from the capacity management system
    """
    return storage_get_country_timeout(country_code)
