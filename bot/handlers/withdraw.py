from pyrogram.client import Client
from pyrogram import filters
from pyrogram import StopPropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, FloodWait
from bot.utils.storage import get_min_withdraw_for_method, try_deduct_balance_for_withdraw, save_withdraw_state, load_withdraw_state, clear_withdraw_state
from bot.data.cards import CARD_NAME_TRANSPORT_MAX, CARD_WITHDRAW_METHOD, is_card_available, is_valid_card_name
from bot.config import get_channel
from datetime import datetime

CHANNEL_WITHDRAW_REPORT = get_channel("CHANNEL_WITHDRAW_REPORT")
from bot.handlers.admin.router import NON_TEXT_INPUT_COMMANDS
import asyncio
import re
import threading
import logging
from typing import Tuple
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)

withdraw_sessions = {}
withdraw_lock = threading.RLock()


def validate_wallet_address(address: str, method: str) -> Tuple[bool, str]:
    """
    Validate wallet address based on withdrawal method.

    Args:
        address: The wallet address to validate
        method: The withdrawal method (USDT, TRX, BINANCE, CARDNAME)

    Returns:
        Tuple of (is_valid, error_message or validated_address)

    Only SURROUNDING whitespace is trimmed; the value is returned unchanged
    otherwise. Invalid input is rejected with a concise error - never
    silently converted into a different value.
    """
    address = address.strip()

    if not address:
        return False, "Wallet address cannot be empty"

    # P8-FIX: the old generic 5-200 length gate ran BEFORE method-specific
    # validation and silently rejected valid CARDNAME values such as "BT",
    # "AF", "A6" or "7K" (only 2 chars). Every method now owns its exact
    # rule. Only SURROUNDING whitespace is trimmed above - internal
    # characters are never stripped, so invalid input is rejected, not
    # silently repaired. (The exact anchored regexes below also bound the
    # work per message, preserving the P7-69 DoS fix.)
    method_upper = (method or "").upper()

    if method_upper == "USDT":
        if address.startswith("0x") and len(address) == 42:
            if re.match(r'^0x[a-fA-F0-9]{40}$', address):
                return True, address
            return False, "Invalid BEP20 address format. Should be 0x followed by 40 hex characters."

        if address.startswith("T") and len(address) == 34:
            if re.match(r'^T[a-zA-Z0-9]{33}$', address):
                if _tron_address_checksum_valid(address):
                    return True, address
                return False, "Invalid TRC20 address (checksum failed). Double-check the address."
            return False, "Invalid TRC20 address format."

        return False, "Invalid USDT address. Use BEP20 (0x...) or TRC20 (T...) format."

    elif method_upper == "TRX":
        if address.startswith("T") and len(address) == 34:
            if re.match(r'^T[a-zA-Z0-9]{33}$', address):
                if _tron_address_checksum_valid(address):
                    return True, address
                return False, "Invalid TRX address (checksum failed). Double-check the address."
            return False, "Invalid TRX address format."
        return False, "Invalid TRX address. Should start with 'T' and be 34 characters."

    elif method_upper == "BINANCE":
        # P8-FIX: exact rule - 9 or 10 digits, nothing else. The old broad
        # pattern accepted usernames, emails and symbols.
        if re.match(r'^\d{9,10}$', address):
            return True, address
        return False, "Invalid Binance ID. Must be 9 or 10 digits only."

    elif method_upper == CARD_WITHDRAW_METHOD:
        # P8-FIX: exact business rule - minimum 2 characters, uppercase A-Z
        # and digits only, at least one uppercase letter required (digits
        # alone are invalid). No BUSINESS maximum; CARD_NAME_TRANSPORT_MAX
        # is a separate, documented Telegram-transport bound (screens and
        # reports embed the full name). No spaces, dots, hyphens,
        # underscores, lowercase or special characters.
        # Single source of truth: the shared CARD_NAME_PATTERN lives in
        # bot.data.cards so user-side and admin-side validation can never
        # drift apart.
        if len(address) > CARD_NAME_TRANSPORT_MAX:
            return False, "Card name is too long (Telegram display limit). Send a shorter name."
        if is_valid_card_name(address):
            return True, address
        return False, "Invalid card name. Use 2+ characters: uppercase A-Z and digits, with at least one letter."

    # Unknown method: legacy behavior preserved (bounded + sanitized).
    if len(address) > 200:
        return False, "Wallet address is too long (max 200 characters)"

    dangerous_chars = ['<', '>', '"', "'", '\\', '\n', '\r', '\t', ';', '`', '$', '|', '&']
    for char in dangerous_chars:
        address = address.replace(char, '')

    return True, address


def _tron_address_checksum_valid(address: str) -> bool:
    """Verify the Base58Check-style checksum of a TRON (T...) address.

    P7-69: the format regex alone accepted every T<33 chars> string - a single
    typo silently redirected the payout to a dead address. TRON addresses are
    0x41-prefixed EIP-55-style hex encoded in Base58Check; without any external
    dependency we re-derive the hex and verify the last 4 bytes are the
    SHA-256(SHA-256(payload)) checksum. Never raises.
    """
    try:
        import hashlib

        # Base58 decode (bitcoin alphabet, not base32!).
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        num = 0
        for ch in address:
            idx = alphabet.find(ch)
            if idx < 0:
                return False
            num = num * 58 + idx
        raw = num.to_bytes((num.bit_length() + 7) // 8, "big")
        # Leading '1' characters are leading zero bytes.
        pad = len(address) - len(address.lstrip("1"))
        raw = b"\x00" * pad + raw

        if len(raw) != 25 or raw[0] != 0x41:
            return False
        payload, checksum = raw[:21], raw[21:]
        derived = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        return derived == checksum
    except Exception:
        return False

async def register_withdraw(client: Client, message: Message):
    """
    Handles /withdraw command - WITH CHANNEL GUARD
    Shows user balance and withdrawal options
    """
    user_id = message.chat.id
    
    from bot.utils.fast_cache import (
        is_user_banned_fast,
        get_cached_template,
        get_user_balance_fast,
        is_withdraw_enabled_fast,
        background_refresh_withdraw_status,
        sync_user_from_storage,
        format_message_fast
    )
    from bot.utils.i18n import get_user_lang, user_t

    sync_user_from_storage(user_id)
    
    if is_user_banned_fast(user_id):
        await message.reply(get_cached_template("banned_user_message", get_user_lang(user_id)))
        return
    
    if not is_withdraw_enabled_fast():
        await message.reply(get_cached_template("withdraw_disabled_message", get_user_lang(user_id)))
        spawn_background(background_refresh_withdraw_status(), "withdraw-status-refresh")
        return
    
    balance = get_user_balance_fast(user_id)
    
    from bot.utils.storage import get_user_withdraw_config
    user_config = get_user_withdraw_config(user_id)
    
    if user_config.get("disabled"):
        await message.reply(user_t("withdraw_disabled_by_admin", user_id))
        return
    
    if balance <= 0:
        await message.reply(get_cached_template("no_balance_message", get_user_lang(user_id)))
        return
    
    text = format_message_fast("withdraw_method_message", get_user_lang(user_id), balance=balance)
    buttons = [
        [InlineKeyboardButton("USDT (BEP20)", callback_data="withdraw_usdt")],
        [InlineKeyboardButton("TRX", callback_data="withdraw_trx")],
        [InlineKeyboardButton("CARD NAME", callback_data="withdraw_cardname")],
        [InlineKeyboardButton("Binance", callback_data="withdraw_binance")]
    ]
    await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))
    
    with withdraw_lock:
        withdraw_sessions[user_id] = {"balance": balance, "step": "waiting_method"}
        # Persist so the flow survives a bot restart.
        save_withdraw_state(user_id, withdraw_sessions[user_id])


def register_withdraw_handlers(app: Client):
    """Register all withdraw-related handlers - WITH CHANNEL GUARD"""

    @app.on_callback_query(filters.regex(r"^withdraw_(usdt|trx|cardname|binance)$"))
    async def handle_withdraw_selection(client: Client, query: CallbackQuery):
        """Withdraw selection - channel membership is enforced by the universal guard.

        CRITICAL: this handler must ALWAYS answer the callback query, otherwise the
        button on the user's client keeps spinning and no message ever appears.
        """
        user_id = query.from_user.id
        from bot.utils.i18n import user_t
        try:
            with withdraw_lock:
                if user_id not in withdraw_sessions:
                    # Rehydrate an in-progress flow persisted before a restart.
                    saved = load_withdraw_state(user_id)
                    if not saved:
                        await query.answer(user_t("withdraw_start_alert", user_id), show_alert=True)
                        return
                    withdraw_sessions[user_id] = saved

                data_str = str(query.data) if query.data else "withdraw_unknown"
                parts = data_str.split("_")
                method = parts[1].upper() if len(parts) > 1 else "UNKNOWN"

                from bot.utils.fast_cache import get_user_balance_fast
                current_balance = get_user_balance_fast(user_id)

                withdraw_sessions[user_id]["balance"] = current_balance

                min_amount = get_min_withdraw_for_method(method.lower())

                from bot.utils.storage import get_user_withdraw_config
                user_config = get_user_withdraw_config(user_id)

                custom_min = user_config.get("min")
                if custom_min is not None:
                    min_amount = custom_min

                custom_max = user_config.get("max")
                if custom_max is not None and current_balance > custom_max:
                    await query.answer(user_t("withdraw_max_alert", user_id, max_amount=custom_max), show_alert=True)
                    return

                if current_balance < min_amount:
                    await query.answer(user_t("withdraw_insufficient_alert", user_id, min_amount=min_amount, current_balance=current_balance), show_alert=True)
                    return

                if current_balance <= 0:
                    await query.answer(user_t("withdraw_no_balance_alert", user_id), show_alert=True)
                    return

                withdraw_sessions[user_id]["method"] = method
                withdraw_sessions[user_id]["step"] = "waiting_wallet"
                save_withdraw_state(user_id, withdraw_sessions[user_id])

            # Answer FIRST so the button loading state always clears, then edit.
            await query.answer()
            from bot.utils.fast_cache import format_message_fast as fmt_msg
            from bot.utils.i18n import get_user_lang
            _lang = get_user_lang(user_id)
            try:
                await query.message.edit_text(fmt_msg("enter_wallet_message", _lang, method=method))
            except MessageNotModified:
                pass
            except FloodWait as fw:
                wait = int(fw.value) if isinstance(fw.value, (int, float)) else 2
                logger.warning(f"Withdraw selection FloodWait: waiting {wait}s before edit")
                await asyncio.sleep(wait)
                try:
                    await query.message.edit_text(fmt_msg("enter_wallet_message", _lang, method=method))
                except Exception as retry_err:
                    logger.warning(f"Withdraw selection edit retry failed: {retry_err}")
            except Exception as edit_err:
                logger.warning(f"Withdraw selection edit failed (query already answered): {edit_err}")
        except Exception as e:
            logger.error(f"ERROR in handle_withdraw_selection: {e}", exc_info=True)
            try:
                await query.answer(user_t("withdraw_error_alert", query.from_user.id), show_alert=True)
            except Exception:
                pass

    @app.on_message(filters.private & filters.text & ~filters.command(NON_TEXT_INPUT_COMMANDS))
    async def handle_wallet_input(client: Client, message: Message):
        """Wallet input - channel membership is enforced by the universal guard."""
        user_id = message.chat.id
        from bot.utils.i18n import user_t, get_user_lang
        try:
            with withdraw_lock:
                if user_id not in withdraw_sessions or withdraw_sessions[user_id].get("step") != "waiting_wallet":
                    # Rehydrate an in-progress flow persisted before a restart.
                    if user_id not in withdraw_sessions:
                        saved = load_withdraw_state(user_id)
                        if saved and saved.get("step") == "waiting_wallet":
                            withdraw_sessions[user_id] = saved
                        else:
                            return
                    else:
                        return

                wallet_address = message.text.strip()

                if user_id not in withdraw_sessions:
                    return

                data = withdraw_sessions[user_id]
                method = data.get("method", "UNKNOWN")

                is_valid, result = validate_wallet_address(wallet_address, method)

                if not is_valid:
                    from bot.utils.fast_cache import format_message_fast as fmt_msg
                    await message.reply(fmt_msg("invalid_wallet_message", get_user_lang(user_id), error=result, method=method))
                    message.stop_propagation()
                    return

                wallet_address = result

                # P8-FIX: only ACTIVE card names may be withdrawn with. The
                # registry gate runs BEFORE any balance check or deduction -
                # an unknown or blocked card never deducts, never creates a
                # request and never counts toward card statistics. The
                # session stays in the waiting_wallet step so the user can
                # simply send a corrected card name.
                if method.upper() == CARD_WITHDRAW_METHOD:
                    from bot.data.cards import is_card_available
                    if not is_card_available(wallet_address):
                        await message.reply(
                            user_t("card_unavailable_message", user_id)
                        )
                        message.stop_propagation()
                        return

                # Re-validate the CURRENT balance before deducting. The balance may
                # have changed since the method was selected (e.g. a new session was
                # approved), and the withdraw flow deducts the full available amount.
                from bot.utils.fast_cache import get_user_balance_fast
                current_balance = get_user_balance_fast(user_id)

                min_amount = get_min_withdraw_for_method(method.lower())
                from bot.utils.storage import get_user_withdraw_config
                user_config = get_user_withdraw_config(user_id)
                custom_min = user_config.get("min")
                if custom_min is not None:
                    min_amount = custom_min

                # P7-69: recheck custom_max at the WALLET stage too. The old
                # code checked it only at method selection - a balance change
                # (or admin config change) between the two steps let a user
                # withdraw more than the configured per-withdraw cap.
                custom_max = user_config.get("max")
                if custom_max is not None and current_balance > custom_max:
                    await message.reply(
                        user_t("withdraw_max_alert", user_id, max_amount=custom_max)
                    )
                    message.stop_propagation()
                    return

                if current_balance <= 0 or current_balance < min_amount:
                    await message.reply(
                        user_t("withdraw_failed_insufficient", user_id, min_amount=min_amount, current_balance=current_balance)
                    )
                    message.stop_propagation()
                    return

                # F-6 FIX: the balance deduction AND the pending request record
                # now commit together in one atomic data-layer call. The old
                # flow deducted here and recorded inside the spawned background
                # task - a crash between the two steps silently swallowed the
                # user's money (balance gone, no request anywhere).
                from bot.data.withdrawals import (
                    build_country_breakdown,
                    create_withdraw_request_atomic,
                )
                breakdown = build_country_breakdown(user_id, expected_total=current_balance)
                created = create_withdraw_request_atomic(
                    user_id, method, current_balance, wallet_address,
                    country_breakdown=breakdown,
                )
                if not created.get("success"):
                    await message.reply(user_t("withdraw_failed_generic", user_id))
                    message.stop_propagation()
                    return

                balance = current_balance
                request_id = created.get("request_id")
                data = withdraw_sessions.pop(user_id)
                method = data["method"]
                clear_withdraw_state(user_id)

            from bot.utils.fast_cache import format_message_fast
            await message.reply(format_message_fast("withdraw_success_message", get_user_lang(user_id), amount=balance, method=method))

            spawn_background(
                process_withdraw_background(client, user_id, balance, method, wallet_address, breakdown, request_id),
                "withdraw-process",
            )

            message.stop_propagation()
        except StopPropagation:
            raise
        except Exception as e:
            logger.error(f"ERROR in handle_wallet_input: {e}", exc_info=True)
            try:
                await message.reply(user_t("withdraw_wallet_error", message.chat.id))
            except Exception:
                pass
            try:
                message.stop_propagation()
            except Exception:
                pass


async def process_withdraw_background(client: Client, user_id: int, balance: float, method: str, wallet_address: str, breakdown=None, request_id: str = None):
    """Notify-only withdraw processing (F-6 FIX).

    The balance deduction AND the pending request record are already committed
    atomically by ``create_withdraw_request_atomic`` in handle_wallet_input.
    Nothing here may refund: the recorded request is settleable by the admin
    (approve/reject), so a notification failure must never silently reverse
    money. ``breakdown`` is the payment-accurate snapshot built before the
    deduction (same tracker state)."""
    try:
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"Processing withdraw: user={user_id}, amount=${balance}, method={method}")

        try:
            from bot.utils import database as db
            await db.log_withdrawal(user_id, method, balance, wallet_address, "pending")
            logger.info(f"Withdraw logged to PostgreSQL database")
        except Exception as db_err:
            logger.warning(f"DB logging unavailable: {db_err}")

        from bot.data.withdrawals import format_country_breakdown
        bd_text = format_country_breakdown(breakdown, include_header=True)
        
        # Transaction No at top, bold
        txn_line = f"**🆔 Trans No : `{request_id}`**" if request_id else ""
        withdraw_message = (
            f"💸 *New Withdraw Request*\n\n"
            f"{txn_line}\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💰 Amount: *${balance}*\n"
            f"💳 Method: *{method}*\n"
            f"📬 Wallet: `{wallet_address}`\n"
            f"⏰ Time: {current_time}"
        )
        if bd_text:
            withdraw_message += f"\n\n{bd_text}"
        
        # Inline buttons for approve/reject
        buttons = None
        if request_id:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"ch_approve_{request_id}"),
                 InlineKeyboardButton("❌ Reject", callback_data=f"ch_reject_{request_id}")]
            ])
        
        report_channel = get_channel("CHANNEL_WITHDRAW_REPORT")
        if report_channel:
            try:
                logger.info(f"Sending withdraw report to channel: {report_channel}")
                from pyrogram.errors import FloodWait
                for attempt in range(3):
                    try:
                        await client.send_message(
                            chat_id=report_channel,
                            text=withdraw_message,
                            reply_markup=buttons
                        )
                        logger.info("Withdraw report sent to channel successfully")
                        break
                    except FloodWait as fw:
                        wait = int(fw.value) if isinstance(fw.value, (int, float)) else 2
                        logger.warning(f"FloodWait: waiting {wait}s before retry")
                        await asyncio.sleep(wait)
                    except Exception as attempt_err:
                        if attempt < 2:
                            logger.warning(f"Retry {attempt+1}/3: channel send failed: {attempt_err}")
                            await asyncio.sleep(0.5)
                        else:
                            raise
            except Exception as channel_error:
                logger.error(f"Failed to send withdraw report to channel after retries: {channel_error}")
        else:
            logger.warning("CHANNEL_WITHDRAW_REPORT not configured")
            
        from bot.utils.fast_cache import sync_user_from_storage_async
        await sync_user_from_storage_async(user_id)
        logger.info(f"User cache synced after withdraw for user {user_id}")

    except Exception as e:
        # F-6: the request is already recorded (committed synchronously before
        # this task started), so a notification/db failure must NOT refund.
        # The pending request is settleable by the admin (approve/reject).
        logger.error(f"Error processing withdraw: {e}", exc_info=True)


# ============================================================
# CHANNEL CALLBACK HANDLERS - Approve/Reject from channel message
# ============================================================

def register_channel_withdraw_handlers(app: Client):
    """Register channel-based withdraw approve/reject handlers."""
    logger.info("🔧 Registering channel withdraw handlers...")

    @app.on_callback_query(filters.regex(r"^ch_approve_(W[A-Z0-9]{7})$", re.IGNORECASE))
    async def channel_approve_callback(client: Client, query: CallbackQuery):
        """Approve withdraw request from channel message."""
        logger.info(f"📥 Channel APPROVE callback received: {query.data}")
        await query.answer("⏳ Processing...")
        try:
            from bot.utils.storage import approve_withdraw_request
            from bot.utils.fast_cache import invalidate_user_cache
            
            match = query.matches[0] if query.matches else None
            if not match:
                logger.warning(f"❌ No regex match for: {query.data}")
                await query.answer("❌ Invalid request", show_alert=True)
                return
            
            request_id = match.group(1).upper()  # Normalize to uppercase for exact DB match
            logger.info(f"✅ Extracted request_id: {request_id}")
            result = approve_withdraw_request(request_id)
            
            if result.get("success"):
                user_id = result.get("user_id", "N/A")
                amount = float(result.get("amount") or 0)
                method = result.get("method", "N/A")
                wallet = result.get("wallet_address", "N/A")
                timestamp = result.get("timestamp", "")
                breakdown = result.get("country_breakdown")
                
                from bot.data.withdrawals import format_country_breakdown
                bd_text = format_country_breakdown(breakdown, include_header=True)
                
                try:
                    invalidate_user_cache(int(user_id))
                except (ValueError, TypeError):
                    pass
                
                # Edit message in place - remove buttons, add APPROVED badge
                approved_text = (
                    f"💸 *New Withdraw Request*\n\n"
                    f"**🆔 Trans No : `{request_id}`**\n\n"
                    f"👤 User ID: `{user_id}`\n"
                    f"💰 Amount: *${amount:.2f}*\n"
                    f"💳 Method: *{method}*\n"
                    f"📬 Wallet: `{wallet}`\n"
                    f"⏰ Time: {timestamp}\n\n"
                    f"✅ **APPROVED**\n"
                    f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                if bd_text:
                    approved_text += f"\n\n{bd_text}"
                
                await query.message.edit_text(approved_text, reply_markup=None)
                
                # Send to admin log channel if configured
                from bot.config import get_channel
                log_channel = get_channel("CHANNEL_WITHDRAW_REPORT")
                if log_channel:
                    try:
                        await client.send_message(chat_id=log_channel, text=approved_text)
                    except Exception as e:
                        logger.error(f"Log channel send failed: {e}")
                        
            else:
                err = result.get("error", "Unknown error")
                logger.warning(f"❌ Approve failed: {err}")
                if err == "Request not found":
                    await query.answer("⚠️ Already settled - refresh message", show_alert=True)
                else:
                    await query.answer(f"Error: {err}", show_alert=True)
        except Exception as e:
            logger.error(f"Channel approve error: {e}", exc_info=True)
            await query.answer(f"Error: {str(e)}", show_alert=True)

    @app.on_callback_query(filters.regex(r"^ch_reject_(W[A-Z0-9]{7})$", re.IGNORECASE))
    async def channel_reject_callback(client: Client, query: CallbackQuery):
        """Reject withdraw request from channel message."""
        logger.info(f"📥 Channel REJECT callback received: {query.data}")
        await query.answer("⏳ Processing...")
        try:
            from bot.utils.storage import reject_withdraw_request
            from bot.utils.fast_cache import invalidate_user_cache
            
            match = query.matches[0] if query.matches else None
            if not match:
                logger.warning(f"❌ No regex match for: {query.data}")
                await query.answer("❌ Invalid request", show_alert=True)
                return
            
            request_id = match.group(1).upper()  # Normalize to uppercase for exact DB match
            logger.info(f"✅ Extracted request_id: {request_id}")
            result = reject_withdraw_request(request_id)
            
            if result.get("success"):
                user_id = result.get("user_id", "N/A")
                amount = float(result.get("amount") or 0)
                method = result.get("method", "N/A")
                wallet = result.get("wallet_address", "N/A")
                timestamp = result.get("timestamp", "")
                breakdown = result.get("country_breakdown")
                
                from bot.data.withdrawals import format_country_breakdown
                bd_text = format_country_breakdown(breakdown, include_header=True)
                
                try:
                    invalidate_user_cache(int(user_id))
                except (ValueError, TypeError):
                    pass
                
                # Edit message in place - remove buttons, add REJECTED badge
                rejected_text = (
                    f"💸 *New Withdraw Request*\n\n"
                    f"**🆔 Trans No : `{request_id}`**\n\n"
                    f"👤 User ID: `{user_id}`\n"
                    f"💰 Amount: *${amount:.2f}*\n"
                    f"💳 Method: *{method}*\n"
                    f"📬 Wallet: `{wallet}`\n"
                    f"⏰ Time: {timestamp}\n\n"
                    f"❌ **REJECTED**\n"
                    f"🔄 Refunded: *${amount:.2f}*\n"
                    f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                if bd_text:
                    rejected_text += f"\n\n{bd_text}"
                
                await query.message.edit_text(rejected_text, reply_markup=None)
                
                # Send to admin log channel if configured
                from bot.config import get_channel
                log_channel = get_channel("CHANNEL_WITHDRAW_REPORT")
                if log_channel:
                    try:
                        await client.send_message(chat_id=log_channel, text=rejected_text)
                    except Exception as e:
                        logger.error(f"Log channel send failed: {e}")
                        
            else:
                err = result.get("error", "Unknown error")
                logger.warning(f"❌ Reject failed: {err}")
                if err == "Request not found":
                    await query.answer("⚠️ Already settled - refresh message", show_alert=True)
                else:
                    await query.answer(f"Error: {err}", show_alert=True)
        except Exception as e:
            logger.error(f"Channel reject error: {e}", exc_info=True)
            await query.answer(f"Error: {str(e)}", show_alert=True)

    logger.info("✅ Channel withdraw handlers registered")
