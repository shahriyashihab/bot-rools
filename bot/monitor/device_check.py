"""Device check: scan an account's active authorizations for extra devices."""
import logging
from typing import List, Tuple

from bot.utils.anti_spam import pre_device_check_delay
from bot.utils.session_handler import get_rotated_api
from bot.verification.device import (
    live_device_verdict,
    VERDICT_SUCCESS,
    VERDICT_MULTI_DEVICE,
    VERDICT_REJECTED,
    VERDICT_UNKNOWN,
)

logger = logging.getLogger(__name__)


async def run_device_check(
    session_string: str,
    country_code: str,
    phone: str = "unknown",
    has_known_extra_devices: bool = False,
    preferred_proxy_url: str = None,
) -> Tuple[bool, List[dict], int, str]:
    """Live extra-device check for the monitor full-validation path.

    Returns ``(has_extra_devices, extra_devices_list, device_count, verdict)``
    where ``verdict`` is one of the ``VERDICT_*`` constants.

    This is a thin wrapper over ``live_device_verdict``. There is NO
    conservative guessing anymore: on a transient failure the verdict is
    UNKNOWN and the caller must keep the session pending instead of inventing
    MULTI-DEVICE (which would wrongly block a balance) or SUCCESS.

    ``has_known_extra_devices`` must be True when the session was ALREADY
    flagged multi-device. The admin "device check" toggle then cannot be used to
    skip the check: skipping returned ``(False, [], 1, VERDICT_SUCCESS)``, i.e.
    a synthetic clean verdict, so a session whose extra devices were never
    logged out got credited at the 24h mark with the seller's own devices still
    signed in. The toggle legitimately controls whether NEW accounts are
    screened for extra devices; it must never retro-cancel the 24h cleanup
    guarantee for an account already known to have them.
    """
    from bot.utils.storage import get_country_setting_bool
    if not get_country_setting_bool(country_code, "device_check"):
        if not has_known_extra_devices:
            print(f"📱 DEVICE CHECK DISABLED: skipping device check for {phone} ({country_code})")
            return False, [], 1, VERDICT_SUCCESS
        print(f"📱 DEVICE CHECK DISABLED but {phone} is already flagged multi-device "
              f"- running the check anyway (24h cleanup must be verified)")

    await pre_device_check_delay(country_code)

    api_id, api_hash, _ = get_rotated_api()
    result = await live_device_verdict(
        session_string=session_string,
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        country_code=country_code if country_code else None,
        preferred_proxy_url=preferred_proxy_url,
    )
    verdict = result["verdict"]

    if verdict == VERDICT_SUCCESS:
        return False, [], 1, verdict
    if verdict == VERDICT_MULTI_DEVICE:
        return True, result["extra_devices"], int(result["device_count"] or 2), verdict
    if verdict == VERDICT_REJECTED:
        return False, [], 1, verdict
    # UNKNOWN: no definitive answer. NEVER report has_extra_devices=True - the
    # verdict field is the source of truth; callers must keep the session
    # pending on UNKNOWN. A false True here would route a session to the
    # multi-device branch and wrongly block a balance.
    return False, [], 0, verdict
