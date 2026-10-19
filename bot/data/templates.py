"""Refactored from bot.utils.storage: Message template storage and formatting."""
from bot.data.state import Any, Dict, _mark_settings_key_dirty, _storage, save_storage, thread_safe
from bot.data.languages import DEFAULT_LANGUAGE

def _custom_entry(template_name: str) -> dict:
    """Return the per-language custom template map for a template (or ``{}``).

    Backwards compatible: a legacy flat string entry is treated as the
    English-only custom template.
    """
    custom = _storage.get("message_templates", {})
    entry = custom.get(template_name)
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        return {DEFAULT_LANGUAGE: entry}
    return {}

def _template_source_lang(template_name: str) -> str:
    """Return the language the admin last uploaded a template in.

    Recorded by the auto-translation service when a template is saved through
    the admin panel. Falls back to English when unknown.
    """
    try:
        meta = _storage.get("message_templates_meta") or {}
        entry = meta.get(template_name)
        if isinstance(entry, dict):
            source = entry.get("source")
            if source in _storage.get("message_templates", {}).get(template_name, {}):
                return source
    except Exception:
        pass
    return DEFAULT_LANGUAGE

def get_message_template(template_name: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """Get a message template for a language.

    Once the admin customizes a template in ANY language, that custom version
    is the source of truth for EVERY language. Per-language overrides are
    honored; languages that have not been translated yet fall back to the
    source language the admin wrote the template in, then English.

    Resolution order for a customized template:
      1. admin-custom template for ``lang`` (per-language override)
      2. admin-custom template in the source language
      3. admin-custom English template
      4. any other admin-custom language

    Resolution order for a stock (never customized) template:
      1. built-in translation dictionary for ``lang``
      2. built-in translation dictionary English entry
      3. built-in default English text
    """
    entry = _custom_entry(template_name)
    if lang in entry:
        return entry[lang]
    if entry:
        source = _template_source_lang(template_name)
        if source in entry:
            return entry[source]
        if DEFAULT_LANGUAGE in entry:
            return entry[DEFAULT_LANGUAGE]
        return next(iter(entry.values()))
    try:
        from bot.data.translations import TRANSLATIONS
        by_lang = TRANSLATIONS.get(template_name)
        if by_lang:
            text = by_lang.get(lang)
            if text is not None:
                return text
            text = by_lang.get(DEFAULT_LANGUAGE)
            if text is not None:
                return text
    except (ImportError, KeyError):
        pass
    return DEFAULT_MESSAGE_TEMPLATES.get(template_name, f"[{template_name}]")

def set_message_template(template_name: str, template_text: str, lang: str = DEFAULT_LANGUAGE) -> None:
    """Set a custom message template for a language (admin override).

    The template becomes admin-customized: ``get_message_template`` will show
    this version to users of every language (falling back to the source
    language until the auto-translation fills the other languages).
    """
    if "message_templates" not in _storage:
        _storage["message_templates"] = {}
    entry = _custom_entry(template_name)
    entry[lang] = template_text
    _storage["message_templates"][template_name] = entry
    _mark_settings_key_dirty("message_templates")
    save_storage()

    try:
        from bot.utils.fast_cache import preload_templates as _refresh
        _refresh()
    except (ImportError, Exception):
        pass

def _drop_template_meta(template_name: str) -> None:
    """Remove the auto-translation metadata for a template, if any."""
    meta = _storage.get("message_templates_meta")
    if meta and isinstance(meta, dict):
        meta.pop(template_name, None)
        _storage["message_templates_meta"] = meta
        _mark_settings_key_dirty("message_templates_meta")

def reset_message_template(template_name: str, lang: str = None) -> None:
    """Reset a message template (one language, or all) to default.

    ``lang=None`` removes the whole custom template. Passing a language code
    removes only that language's override, keeping other languages' custom text.

    Resetting the source language (the one the template was last written in)
    also drops the stale auto-translations that were derived from it, keeping
    only languages the admin edited by hand - otherwise those languages would
    keep showing text translated from the old source.
    """
    entry = _custom_entry(template_name)
    if not entry:
        return
    if lang is None:
        if "message_templates" in _storage:
            _storage["message_templates"].pop(template_name, None)
        _drop_template_meta(template_name)
    else:
        source = _template_source_lang(template_name)
        entry.pop(lang, None)
        if lang == source:
            try:
                meta = _storage.get("message_templates_meta") or {}
                meta_entry = meta.get(template_name) or {}
                manual = set(meta_entry.get("manual") or [])
            except Exception:
                manual = set()
            kept = {k: v for k, v in entry.items() if k in manual}
            if kept:
                _storage["message_templates"][template_name] = kept
            else:
                _storage["message_templates"].pop(template_name, None)
            _drop_template_meta(template_name)
        elif entry:
            _storage["message_templates"][template_name] = entry
        else:
            _storage["message_templates"].pop(template_name, None)
            _drop_template_meta(template_name)
    _mark_settings_key_dirty("message_templates")
    save_storage()

    try:
        from bot.utils.fast_cache import preload_templates as _refresh
        _refresh()
    except (ImportError, Exception):
        pass

def get_all_message_templates() -> Dict[str, Dict[str, Any]]:
    """Get all message templates with their status."""
    result = {}

    for name, default_text in DEFAULT_MESSAGE_TEMPLATES.items():
        entry = _custom_entry(name)
        is_custom = bool(entry)
        current_text = get_message_template(name) if is_custom else default_text
        result[name] = {
            "current": current_text,
            "default": default_text,
            "is_custom": is_custom,
            "custom": entry,
        }

    return result

def format_message(template_name: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Format a message template with variables (safe - no overlapping key corruption)."""
    import re
    template = get_message_template(template_name, lang)
    try:
        for key, value in kwargs.items():
            template = re.sub(r'\{' + re.escape(key) + r'\}', lambda m: str(value), template)
        return template
    except Exception:
        return template

DEFAULT_MESSAGE_TEMPLATES = {
    "account_received_message": (
        "\u2705 *Account Received*\n\n"
        "\U0001f4f1 `{phone}`   {flag}\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\U0001f4b0 Price        \u2192  *Calculating...*\n"
        "\U0001f4ca Status       \u2192  \U0001f504 Processing\n"
        "\u23f0 Received     \u2192  {received_time}\n"
        "\u23f0 ETA          \u2192  ~{eta_min_sec}\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        "\U0001f514 You will be notified when verified."
    ),
    "success_message": (
        "\u2705 *Verification Successful*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n"
        "\U0001f4b0 Balance Added: *${price}*\n"
        "\U0001f4ca Status: *{status}*\n\n"
        "Your account has been credited successfully."
    ),
    "multi_device_message": (
        "\u26a0\ufe0f *Multiple Devices Detected*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n\n"
        "Multiple devices are logged into this account.\n"
        "The session may be invalid.\n\n"
        "Use /start to try a different number."
    ),
    "support_message": (
        "\U0001f4de *Support*\n\n"
        "Contact the admin for assistance.\n"
        "Please describe your issue clearly."
    ),
    "account_stats_message": (
        "\U0001f194 ID : {user_id}\n\n"
        "\U0001f4f1 Number of verified accounts : {verified}\n"
        "\U0001f4f1 Number of unverified accounts : {unverified}\n"
        "\U0001f4b8 Available Balance IN USDT : {available_balance}\n"
        "\U0001f9ca Number of frozen account : {frozen}\n"
        "\u2744\ufe0f Total Frozen Balance : {frozen_balance}\n\n"
        "\U0001f4c5 Date : {date}\n"
        "\u23f0 Time : {time}\n\n"
        "\U0001f4dd Note : Work, be honest Upgrade your work and earn money and enjoy your life\n     /start"
    ),
    "account_info_message": (
        "\U0001f3b0 *Your Account*\n\n"
        "\U0001f464 User ID: `{user_id}`\n"
        "\U0001f4b0 Balance: *${balance}*\n"
        "\u2705 Sessions: *{sessions}*\n"
        "\U0001f4c5 Joined: *{joined}*\n"
        "\U0001f550 Time: *{time}*\n\n"
        "Commands:\n"
        "/start - Submit a number\n"
        "/withdraw - Withdraw funds\n"
        "/cap - View open countries"
    ),
    "verification_failed_message": (
        "\u274c *Verification Failed*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n"
        "\U0001f30d Country: {flag} {country_code}\n\n"
        "Device is not logged in. Session has expired.\n\n"
        "Type /start to try again."
    ),
    "welcome_message": (
        "\U0001f44b *Welcome!*\n\n"
        "Send your phone number to begin verification.\n\n"
        "Example: `+8801XXXXXXXXXX`\n\n"
        "Commands:\n"
        "/account - View your account\n"
        "/withdraw - Withdraw balance\n"
        "/cap - Available countries\n"
        "/cancel - Stop verification"
    ),
    "invalid_otp_message": (
        "\u274c *Invalid OTP Code*\n\n"
        "The code you entered is incorrect.\n"
        "Please enter the OTP sent to your Telegram app."
    ),
    "processing_message": (
        "\u23f3 *Processing...*\n\n"
        "Please wait while we process your request.\n"
        "This may take a few moments."
    ),
    "banned_user_message": (
        "\U0001f6ab *Access Denied*\n\n"
        "You have been banned from using this bot.\n"
        "Contact support if you believe this is a mistake."
    ),
    "withdraw_success_message": (
        "\u2705 *Withdraw Request Submitted*\n\n"
        "\U0001f4b0 Amount: *${amount}*\n"
        "\U0001f4b3 Method: *{method}*\n\n"
        "Your request will be processed shortly.\n"
        "Check /account for updated balance."
    ),
    "no_balance_message": (
        "\u274c *Insufficient Balance*\n\n"
        "You do not have any balance to withdraw.\n\n"
        "Submit numbers via /start to earn balance."
    ),
    "twofa_already_active_message": (
        "\u26a0\ufe0f *2FA Already Enabled*\n\n"
        "This account already has Two-Factor Authentication.\n\n"
        "Use /start to try a different number."
    ),

    "enter_phone_message": (
        "\U0001f4f1 *Enter Your Phone Number*\n\n"
        "Send your phone number with country code.\n\n"
        "Example: `+8801XXXXXXXXXX`\n\n"
        "\u26a0\ufe0f Include the correct country code.\n"
        "Type /cancel to stop."
    ),
    "enter_otp_message": (
        "\U0001f522 *Enter OTP Code*\n\n"
        "Enter the code sent to your Telegram app.\n\n"
        "\u23f0 Timeout: *{timeout} seconds*\n\n"
        "Type /cancel to stop."
    ),
    "otp_sent_message": (
        "\U0001f4e4 *OTP Code Sent*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n"
        "\U0001f30d Country: {flag} {country_code}\n\n"
        "\u23f0 Time Remaining: *{timeout} seconds*\n\n"
        "Enter the code or type /cancel"
    ),
    "waiting_verification_message": (
        "\u23f3 *Verification In Progress*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n"
        "\U0001f30d Country: {flag} {country_code}\n\n"
        "\u23f0 Estimated Time: *{timeout} seconds*\n\n"
        "You will be notified when complete."
    ),
    "twofa_setup_message": (
        "\U0001f510 *Setting Up Security*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n\n"
        "\u23f3 2FA is being configured.\n"
        "Please wait approximately 2 minutes."
    ),
    "spam_check_message": (
        "\U0001f50d *Checking Account Status*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n\n"
        "\u23f3 Verifying account standing...\n"
        "This ensures fair pricing."
    ),
    "withdraw_disabled_message": (
        "\u274c *Withdrawals Unavailable*\n\n"
        "Withdrawals are temporarily disabled.\n"
        "Please check back later."
    ),
    "withdraw_method_message": (
        "\U0001f4b3 *Select Withdrawal Method*\n\n"
        "\U0001f4b0 Your Balance: *${balance}*\n\n"
        "Please choose your preferred method:"
    ),
    "enter_wallet_message": (
        "\U0001f4ec *Enter {method} Wallet Address*\n\n"
        "Send your {method} wallet address below.\n\n"
        "\u26a0\ufe0f *Important:*\n"
        "\u2022 Double-check the address before sending\n"
        "\u2022 Incorrect addresses cannot be refunded\n"
        "\u2022 Type /cancel to abort"
    ),
    "invalid_wallet_message": (
        "\u274c *Invalid Wallet Address*\n\n"
        "Error: `{error}`\n\n"
        "\U0001f4ec Please send a valid *{method}* address.\n\n"
        "Type /cancel to abort."
    ),
    "card_unavailable_message": (
        "This card is not available.\n"
        "Please contact your admin.\n"
        "Otherwise, contact this bot support."
    ),
    "session_timeout_message": (
        "\u23f0 *Session Expired*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n"
        "\U0001f30d Country: {flag} {country_code}\n\n"
        "No response received within *{timeout} seconds*.\n\n"
        "\U0001f504 Type /start to try again."
    ),
    "country_not_available_message": (
        "\u274c *Country Unavailable*\n\n"
        "\U0001f30d Country: {flag} {country_code}\n\n"
        "This country is currently closed.\n\n"
        "Type /cap to see available countries\n"
        "Or try a different number with /start"
    ),
    "join_channel_message": (
        "\U0001f4e2 *Channel Required*\n\n"
        "\U0001f517 Channel: {channel}\n\n"
        "You must join this channel to use the bot.\n\n"
        "\u2705 After joining, click the button below\n"
        "or type /start to begin."
    ),
    "min_withdraw_message": (
        "\u26a0\ufe0f *Insufficient Balance*\n\n"
        "\U0001f4b0 Your Balance: *${balance}*\n"
        "\U0001f4b5 Minimum Withdraw: *${min_amount}*\n\n"
        "\u274c You need *${needed}* more to withdraw.\n\n"
        "Submit more numbers via /start"
    ),
    "enter_2fa_password_message": (
        "\U0001f510 *2FA Password Required*\n\n"
        "\U0001f4f1 Phone: `{phone}`\n\n"
        "This account has Two-Factor Authentication.\n\n"
        "\U0001f511 Enter the 2FA password:\n\n"
        "Type /cancel to abort."
    ),
    # MEDIUM-30: the admin's approve/reject decision now reaches the user.
    "withdraw_approved_user": (
        "\u2705 *Withdraw Approved*\n\n"
        "\U0001f4b0 Amount: *${amount}*\n"
        "\U0001f4b3 Method: {method}\n"
        "\U0001f4ec Wallet: `{wallet}`\n\n"
        "\U0001f4b8 The payment is on its way. Check your wallet!"
    ),
    "withdraw_rejected_user": (
        "\u274c *Withdraw Rejected*\n\n"
        "\U0001f4b0 Amount: *${amount}*\n"
        "\U0001f4b3 Method: {method}\n"
        "\U0001f4ec Wallet: `{wallet}`\n\n"
        "\U0001f501 *${amount} has been refunded to your balance.*\n\n"
        "You can submit a new withdraw request anytime."
    )
}
