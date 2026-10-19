"""
API credential rotation for the session verification workflow.
=================================================================
Selects the next API credentials using TRUE ROUND-ROBIN rotation.

CORE CONFIG: a country with a per-country custom API configured (Country
Pricing -> CORE CONFIG -> Set Custom API) uses THOSE credentials for its
login client; every other country keeps the global rotation exactly as
before.
"""

from bot import config
from bot.utils.api_rotation import get_api_manager


def get_rotated_api(country_code: str = None) -> tuple:
    """
    Get next API credentials using TRUE ROUND-ROBIN rotation.

    ``country_code`` (optional): when the country has a custom API stored,
    it is returned INSTEAD of rotating. Rotation is untouched for every
    country without one. Returns: (api_id, api_hash, api_name)
    """
    if country_code:
        try:
            from bot.data.countries import get_country_custom_api
            _custom = get_country_custom_api(country_code)
            if _custom and _custom.get("api_id") and _custom.get("api_hash"):
                return (int(_custom["api_id"]), str(_custom["api_hash"]),
                        f"country-custom:{country_code}")
        except Exception as custom_err:
            print(f"⚠️ Custom API lookup failed for {country_code}: {custom_err}")

    manager = get_api_manager()
    api = manager.get_next_api()

    if api:
        return (api.api_id, api.api_hash, api.name)
    else:
        print("⚠️ No rotated API available, using config default")
        return (config.API_ID, config.API_HASH, "default")
