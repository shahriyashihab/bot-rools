# bot/services/country_profile.py
"""CORE CONFIG: apply a country's uploaded Name/Bio lists to a verified account.

Country Pricing -> CORE CONFIG -> <Country>:
  - [ Upload Name TXT ]  -> names stored for THAT country only
  - [ Upload Bio TXT ]   -> bios stored for THAT country only
  - [ ✅ Name: ON/OFF ]  -> gates the name application for that country
  - [ ✅ Bio: ON/OFF ]   -> gates the bio application for that country

During the verification check phase (while the account client is live), the
account profile is set from the selected country's lists:

  - The pick is DETERMINISTIC per phone number (stable hash), so the same
    account always receives the same name/bio and repeated verification of
    the same number never flips profiles randomly.
  - Telegram limits are respected (first/last name 64 UTF-16 code units, bio
    70 UTF-16 code units - the SERVER counts UTF-16 units, so slicing by
    Python code points would let emoji/astral chars through and the whole
    ``update_profile`` call would fail, silently dropping the bio too; see
    ``_utf16_truncate``).
  - Every failure is swallowed and logged: a profile problem can NEVER break
    a verification session.
  - Every network call is BOUNDED (``op_with_timeout``): a hung MTProto call
    becomes a normal handled error. This matters twice over - the profile
    runs inside the delayed 2FA task, whose semaphore slot is only released
    when the task returns, and a hung ``get_me()`` here previously leaked a
    ``_delayed_task_semaphore`` slot PERMANENTLY (verification-pool
    exhaustion).
  - A country with no list uploaded (or the toggle OFF) is a strict no-op -
    accounts keep whatever profile they already had.
"""

import hashlib
import os
from typing import Dict

from bot.utils.net_timeouts import op_with_timeout

# Telegram profile limits (server-enforced, counted in UTF-16 code units).
_MAX_NAME_LEN = 64
_MAX_BIO_LEN = 70
# Hard ceiling for the whole profile application: three bounded RPCs plus a
# photo upload. Photo upload gets the largest share (media through a proxy).
_PROFILE_OP_TIMEOUT = 60
_PHOTO_UPLOAD_TIMEOUT = 90


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units - what Telegram's server actually counts."""
    return len(text.encode("utf-16-le")) // 2


def _utf16_truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` UTF-16 code units.

    Walks CODE POINTS and accumulates units, so a surrogate pair can never be
    split: an astral character (emoji etc.) costs 2 units and is either kept
    whole or dropped. A naive ``text[:limit]`` cuts by code points - an
    emoji-heavy name would sail past the server limit, and a naive byte cut
    could produce a lone surrogate that pyrogram serializes into invalid
    UTF-8 (the exact failure class ``tests/test_text_surrogates.py`` guards).
    """
    if _utf16_len(text) <= limit:
        return text
    out = []
    units = 0
    for ch in text:
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            break
        out.append(ch)
    return "".join(out)


def _stable_index(phone: str, size: int) -> int:
    """Deterministic index for this phone within a list of ``size`` items."""
    if size <= 0:
        return 0
    digest = hashlib.sha256((phone or "").encode("utf-8", "ignore")).hexdigest()
    return int(digest, 16) % size


def _split_name(full_name: str) -> Dict[str, str]:
    """Split 'First [Middle] Last' into first_name/last_name within limits."""
    parts = [p for p in (full_name or "").strip().split() if p]
    if not parts:
        return {}
    first = _utf16_truncate(parts[0], _MAX_NAME_LEN)
    last = _utf16_truncate(" ".join(parts[1:]), _MAX_NAME_LEN)
    result = {"first_name": first}
    if last:
        result["last_name"] = last
    return result


async def apply_country_profile(client, country_code: str, phone: str) -> Dict[str, str]:
    """Apply name/bio to an authenticated account client for ONE country.

    Returns what was applied, e.g. ``{"name": "Karim", "bio": "Hello"}``.
    Empty dict = nothing applied (toggle OFF / no list / error). Never raises.
    Every network operation is bounded (``op_with_timeout``) so a wedged
    MTProto request can never leak the caller's task/semaphore slot.
    """
    applied: Dict[str, str] = {}
    if client is None or not country_code:
        return applied
    try:
        from bot.data.countries import (
            get_country_setting_bool,
            get_country_name_list,
            get_country_bio_list,
        )

        name_kwargs: Dict[str, str] = {}
        bio_text: str = ""

        if get_country_setting_bool(country_code, "name_enabled"):
            names = get_country_name_list(country_code)
            if names:
                name_kwargs = _split_name(names[_stable_index(phone, len(names))])

        if get_country_setting_bool(country_code, "bio_enabled"):
            bios = get_country_bio_list(country_code)
            if bios:
                bio_text = _utf16_truncate(
                    bios[_stable_index(phone, len(bios))], _MAX_BIO_LEN
                )

        if name_kwargs or bio_text:
            await op_with_timeout(
                client.update_profile(
                    first_name=name_kwargs.get("first_name"),
                    last_name=name_kwargs.get("last_name"),
                    bio=bio_text or None,
                ),
                "profile-update-profile",
                _PROFILE_OP_TIMEOUT,
            )
            if name_kwargs.get("first_name"):
                applied["name"] = " ".join(
                    p for p in (name_kwargs.get("first_name"),
                                name_kwargs.get("last_name")) if p)
            if bio_text:
                applied["bio"] = bio_text
            print(f"👤 PROFILE: applied CORE CONFIG name/bio for {phone} ({country_code}): {applied}")

        # CORE CONFIG: per-country profile photo (sequential rotation, each
        # account gets the NEXT photo; cursor persisted across restarts).
        # PYROGRAM QUIRK (root cause of "'NoneType' object has no attribute
        # 'is_premium'"): pyrogram sets ``self.me`` ONLY inside ``start()``.
        # Check-phase clients are CONNECTED, never STARTED, so ``self.me`` is
        # None - and pyrogram's save_file() (used by set_profile_photo) reads
        # ``self.me.is_premium`` -> crash. ``get_me()`` alone does NOT fix it;
        # the result must be assigned to ``client.me`` explicitly.
        try:
            if not getattr(client, "me", None):
                client.me = await op_with_timeout(
                    client.get_me(), "profile-get-me", _PROFILE_OP_TIMEOUT
                )
        except Exception as me_err:
            print(f"⚠️ PROFILE: get_me failed for {phone}: {me_err}")
        try:
            from bot.data.countries import (
                get_country_photo_enabled,
                get_next_country_photo,
            )
            if get_country_photo_enabled(country_code):
                photo_path = get_next_country_photo(country_code)
                if photo_path:
                    await op_with_timeout(
                        client.set_profile_photo(photo=photo_path),
                        "profile-set-photo",
                        _PHOTO_UPLOAD_TIMEOUT,
                    )
                    applied["photo"] = os.path.basename(photo_path)
                    print(f"🖼️ PROFILE: applied photo for {phone} ({country_code}): {applied['photo']}")
        except Exception as photo_err:
            print(f"⚠️ PROFILE: could not apply photo for {phone} ({country_code}): {photo_err}")
    except Exception as e:
        # A profile glitch must never affect the verification outcome.
        print(f"⚠️ PROFILE: could not apply name/bio for {phone} ({country_code}): {e}")
    return applied
