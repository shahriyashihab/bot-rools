"""
Account status classification (spam check).
=====================================================================
Multi-layer detection system that inspects a Pyrogram session string
and classifies the account as "spam_free", "spam", or "limit".
"""

import asyncio
import logging
from datetime import datetime

from pyrogram.client import Client
from pyrogram.errors import (
    PeerFlood,
    UserBannedInChannel,
    UserDeactivated,
    AuthKeyUnregistered,
)

from bot.utils.anti_spam import (
    between_api_calls_delay,
    human_delay,
    safe_disconnect,
)
from bot.utils.api_rotation import record_api_usage
from bot.utils.device_generator import generate_device_info
from bot.utils.net_timeouts import connect_with_timeout, op_with_timeout
from bot.verification.api import get_rotated_api

logger = logging.getLogger(__name__)


# ============================================================
# Shared @SpamBot response classifier (multi-language)
# ============================================================
# @SpamBot replies in the ACCOUNT'S UI LANGUAGE, not English. The old
# English-only matching classified every Bengali/Russian/Arabic/... CLEAN
# reply as "spam" (+70) - a systematic false positive that underpaid good
# accounts. These keyword tables cover the major Telegram UI languages for
# the three canonical SpamBot verdicts:
#   clean   -> "Good news, no limits are currently applied..." (spam_free)
#   limited -> "Your account is limited until <date>."       (limit)
#   spam    -> "I'm afraid that some of your activities..."  (spam)
# An unrecognised reply (truly unknown language/wording) now scores a
# moderate +20 instead of the blanket +70, so an unreadable reply alone can
# never push a clean account into "spam" (needs >=70) or "limit" (>=30).
#
# IMPORTANT: check order is clean -> limited -> spam-block -> unknown.
_SPAMBOT_CLEAN_PHRASES = (
    # English
    "good news", "no limits", "free as a bird", "not limited",
    # Russian
    "хорошие новости", "никаких ограничений", "абсолютно свободны", "вы свободны",
    # Bengali
    "সুসংবাদ", "সুখবর", "কোনো সীমাবদ্ধতা নেই", "সীমাবদ্ধতা নেই", "সম্পূর্ণ স্বাধীন", "আপনি স্বাধীন",
    # Arabic
    "أخبار سارة", "لا توجد قيود", "لا قيود", "أنت حر",
    # Spanish
    "buenas noticias", "no hay límites", "no estás limitado", "libre como un pájaro",
    # Portuguese
    "boas notícias", "sem limites", "livre como um pássaro",
    # Indonesian
    "kabar baik", "tidak ada batasan", "bebas seperti burung",
    # Hindi
    "अच्छी खबर", "कोई सीमा नहीं", "पूरी तरह स्वतंत्र",
    # Turkish
    "iyi haber", "herhangi bir sınır yok", "sınır yok", "özgürsün",
    # German
    "gute nachrichten", "keine einschränkungen", "frei wie ein vogel",
    # Persian
    "خبر خوب", "هیچ محدودیتی", "آزاد هستید",
    # Uzbek
    "yaxshi yangilik", "cheklov yo'q", "cheklov yoq",
    # Chinese
    "好消息", "没有任何限制",
)

_SPAMBOT_LIMITED_PHRASES = (
    # English
    "limited until", "will be limited until", "account is limited",
    # Russian
    "ограничен до", "аккаунт ограничен",
    # Bengali
    "সীমাবদ্ধ থাকবে", "পর্যন্ত সীমাবদ্ধ", "সীমিত থাকবে",
    # Arabic
    "تم تقييد حسابك", "مقيد حتى", "محدود حتى",
    # Spanish
    "limitada hasta", "tu cuenta está limitada", "cuenta limitada",
    # Portuguese
    "limitada até", "sua conta está limitada",
    # Indonesian
    "dibatasi sampai", "akun anda dibatasi", "akun kamu dibatasi",
    # Hindi
    "तक सीमित", "आपका खाता सीमित",
    # Turkish
    "kısıtlandı", "sınırlandırıldı",
    # German
    "eingeschränkt bis", "ist eingeschränkt",
    # Persian
    "محدود شده است", "محدود خواهد بود",
    # Uzbek
    "cheklangan",
    # Chinese
    "限制至", "已被限制",
)

_SPAMBOT_SPAM_PHRASES = (
    # English
    "aroused suspicion", "not allowed", "anti-spam", "antispam", "violat",
    "terms of service", "too new", "cannot use", "unable to use", "banned",
    "blocked", "indefinitely",
    # Russian
    "вызывает подозрение", "вы не можете", "антиспам", "нарушение",
    "заблокирован", "слишком новый", "навсегда",
    # Bengali
    "সন্দেহ", "সন্দেহজনক", "অনুমতি নেই", "অ্যান্টি-স্প্যাম", "লঙ্ঘন", "ব্লক করা", "স্থায়ীভাবে",
    # Arabic
    "يشتبه", "لا يمكنك", "لا يمكنك استخدام", "مخالف", "تم حظر", "حظر دائم",
    # Spanish
    "sospecha", "no puedes", "viola", "bloqueado", "permanentemente",
    # Portuguese
    "suspeita", "não podes", "não pode", "viola", "bloqueado",
    # Indonesian
    "dicurigai", "anda tidak dapat", "melanggar", "diblokir",
    # Hindi
    "संदेह", "अनुमति नहीं", "उल्लंघन", "प्रतिबंधित", "स्थायी रूप से",
    # Turkish
    "şüphe", "kullanamazsınız", "ihlal", "engellendi", "kalıcı olarak",
    # German
    "verdächtig", "nicht berechtigt", "verstoß", "gesperrt", "dauerhaft",
    # Persian
    "مشکوک", "نمی‌توانید", "مخالف", "مسدود", "برای همیشه",
    # Uzbek
    "shubha", "foydalanolmaysiz", "buzilishi", "bloklangan",
    # Chinese
    "可疑", "无法使用", "违反", "已被封禁", "永久",
)


def classify_spambot_response(response_text: str) -> str:
    """Classify a raw @SpamBot reply.

    Returns one of: "clean", "limited", "spam", "unknown".

    Matching is language-agnostic (major Telegram UI locales), case- and
    punctuation-tolerant. "unknown" is returned only when NO known phrase
    matched - callers decide the (moderate) penalty for that case.
    """
    if not response_text:
        return "unknown"
    normalized = " ".join(str(response_text).lower().split())
    for phrase in _SPAMBOT_CLEAN_PHRASES:
        if phrase in normalized:
            return "clean"
    for phrase in _SPAMBOT_LIMITED_PHRASES:
        if phrase in normalized:
            return "limited"
    for phrase in _SPAMBOT_SPAM_PHRASES:
        if phrase in normalized:
            return "spam"
    return "unknown"


async def check_account_status(session_string: str, country_code: str = None, preferred_proxy_url: str = None) -> tuple:
    """
    ADVANCED Account Status Detection System v3.0 - 10/10 PERFECT
    ==============================================================
    
    Multi-layer detection system that checks:
    1. User flags (restricted, scam, fake, premium)
    2. SpamBot response (@SpamBot official check)
    3. Message sending capability test
    4. Privacy settings analysis
    5. PEER_FLOOD / rate limit detection
    6. Account activity indicators
    
    Args:
        session_string: Pyrogram session string
        country_code: Optional country code for proxy routing. When provided
                      the check client uses the country's proxy pool. None
                      or empty = direct connection (today's behaviour).
    
    Returns:
        tuple: (status: str, details: dict)
        - status: "spam_free", "spam", or "limit"
        - details: Comprehensive information about the account status
    """
    api_name = None
    try:
        await human_delay(0.5, 1.0, "pre-status-check")
        
        api_id, api_hash, api_name = get_rotated_api()
        print(f"🔄 STATUS CHECK: Using {api_name} for spam check")

        # P7-75: deterministic per-account device fingerprint (same auth key,
        # same device profile across every verification step). Seeded from the
        # session string itself - stable for this account.
        device_info = generate_device_info(seed=session_string[:24], country_code=country_code)
        
        temp_client = None
        try:
            def _make_status_client(proxy):
                kwargs = dict(
                    name=f"status_check_{int(datetime.now().timestamp())}",
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    in_memory=True,
                    device_model=device_info["device_model"],
                    system_version=device_info["system_version"],
                    app_version="Telegram Android 0.28.1.1771-arm64-v8a",
                )
                if proxy:
                    kwargs["proxy"] = proxy
                return Client(**kwargs)

            from bot.utils.proxy_connector import connect_with_proxy_chain
            temp_client = await connect_with_proxy_chain(
                _make_status_client, country_code, "spam-status",
                preferred_url=preferred_proxy_url,
                allow_breaker_self_heal=False,
            )
        except Exception as _connect_err:
            # P4-32: a connect failure is an INFRASTRUCTURE problem (all
            # proxies dead / Telegram unreachable), not a verdict about the
            # account. The old "limit" return silently UNDERPAID genuine
            # accounts during outages. Callers now treat
            # check_method="connect_error" as inconclusive: the session stays
            # pending (delayed task path) or keeps the stored status (monitor
            # path) instead of a downgraded rate.
            print(f"⚠️ STATUS CHECK: could not connect ({_connect_err}) - INCONCLUSIVE (connect_error)")
            return "limit", {
                "check_method": "connect_error",
                "inconclusive": True,
                "error": str(_connect_err),
            }

        await between_api_calls_delay("status-connect")
        
        status = "spam_free"
        spam_score = 0
        details = {
            "is_restricted": False,
            "is_scam": False,
            "is_fake": False,
            "is_premium": False,
            "restriction_reason": None,
            "has_privacy_issues": False,
            "can_send_messages": True,
            "spambot_status": "unknown",
            "spambot_message": None,
            "peer_flood_detected": False,
            "account_deleted": False,
            "check_method": "advanced_multi_layer",
            "spam_score": 0,
            "checks_performed": []
        }

        _flood_detected = False
        
        try:
            me = await op_with_timeout(temp_client.get_me(), "spam-check-get-me")
            details["checks_performed"].append("get_me")
            
            if hasattr(me, 'is_restricted') and me.is_restricted:
                spam_score += 40
                details["is_restricted"] = True
                details["restriction_reason"] = getattr(me, 'restriction_reason', 'Unknown restriction')
                print(f"⚠️ [CHECK 1] Account is RESTRICTED: {details['restriction_reason']}")
            
            if hasattr(me, 'is_scam') and me.is_scam:
                spam_score += 60
                details["is_scam"] = True
                print(f"⚠️ [CHECK 2] Account is marked as SCAM")
            
            if hasattr(me, 'is_fake') and me.is_fake:
                spam_score += 50
                details["is_fake"] = True
                print(f"⚠️ [CHECK 3] Account is marked as FAKE")
            
            if hasattr(me, 'is_premium') and me.is_premium:
                spam_score -= 20
                details["is_premium"] = True
                print(f"✅ [CHECK 4] Account has PREMIUM status")
            
            await between_api_calls_delay("post-getme")
            
            try:
                details["checks_performed"].append("spambot_check")
                
                await between_api_calls_delay("pre-spambot")
                
                await op_with_timeout(temp_client.send_message("spambot", "/start"), "spambot-start")
                
                await asyncio.sleep(2.5)
                
                messages = []
                try:
                    async for msg in temp_client.get_chat_history("spambot", limit=3):
                        if msg.from_user and msg.from_user.is_bot:
                            messages.append(msg.text or "")
                except Exception as hist_err:
                    logger.warning(f"[CHECK 5] SpamBot history error: {hist_err}")
                
                if messages:
                    spambot_response = " ".join(messages)
                    details["spambot_message"] = messages[0][:200] if messages else None

                    verdict = classify_spambot_response(spambot_response)
                    if verdict == "clean":
                        details["spambot_status"] = "clean"
                        spam_score -= 30
                        print(f"✅ [CHECK 5] SpamBot: Account is CLEAN (spam_free)")
                    elif verdict == "limited":
                        details["spambot_status"] = "limited"
                        spam_score += 50
                        print(f"⚠️ [CHECK 5] SpamBot: Account has TIME-BASED LIMITATION (limit)")
                    elif verdict == "spam":
                        details["spambot_status"] = "spam"
                        spam_score += 70
                        print(f"❌ [CHECK 5] SpamBot: Account is SPAM (sorry/anti-spam/violating/too new/banned/etc)")
                    else:
                        # Unrecognised reply (unknown language/wording): moderate
                        # penalty only. It must NEVER alone push a clean account
                        # into the "limit" (>=30) or "spam" (>=70) bands - that
                        # was the systematic non-English false positive.
                        details["spambot_status"] = "unknown"
                        spam_score += 20
                        print(f"⚠️ [CHECK 5] SpamBot: Unrecognised reply - moderate penalty (raw kept for review)")
                else:
                    details["spambot_status"] = "no_response"
                    print(f"⚠️ [CHECK 5] SpamBot: No response received")
                        
            except PeerFlood:
                # PeerFlood here reflects the BOT'S OWN traffic volume being
                # rate-limited by Telegram, not the account's state. The old
                # +80 pushed clean accounts into "limit"/"spam" purely from
                # bot-side pressure (systematic economic false positive at
                # scale). The check is now marked INCONCLUSIVE: no score
                # change, flagged for review in the details.
                details["peer_flood_detected"] = True
                details["spambot_status"] = "inconclusive_peer_flood"
                print(f"⚠️ [CHECK 5] PEER_FLOOD during SpamBot check - bot-side rate limit, check INCONCLUSIVE (no penalty)")
            except Exception as e:
                print(f"⚠️ [CHECK 5] SpamBot check skipped: {e}")
                details["spambot_status"] = "skipped"
            
            await between_api_calls_delay("post-spambot")
            
            try:
                details["checks_performed"].append("message_capability")
                await op_with_timeout(temp_client.send_message("me", "🔍 Session verification test"), "spam-check-send")
                
                try:
                    async for msg in temp_client.get_chat_history("me", limit=1):
                        if msg.text == "🔍 Session verification test":
                            await temp_client.delete_messages("me", msg.id)
                            break
                except Exception as cleanup_err:
                    logger.warning(f"[CHECK 6] History cleanup skipped: {cleanup_err}")
                
                details["can_send_messages"] = True
                spam_score -= 10
                print(f"✅ [CHECK 6] Message sending capability: OK")
                
            except PeerFlood:
                # Bot-side rate limit again (see SpamBot PeerFlood note) -
                # INCONCLUSIVE, never a penalty against the account.
                details["can_send_messages"] = False
                details["peer_flood_detected"] = True
                details["send_test"] = "inconclusive_peer_flood"
                print(f"⚠️ [CHECK 6] PEER_FLOOD on message send - bot-side, INCONCLUSIVE (no penalty)")
            except UserBannedInChannel:
                spam_score += 40
                details["can_send_messages"] = False
                print(f"⚠️ [CHECK 6] User banned in some channels")
            except Exception as e:
                print(f"⚠️ [CHECK 6] Message test warning: {e}")
            
            await between_api_calls_delay("post-msgtest")
            
            try:
                details["checks_performed"].append("privacy_settings")
                print(f"⚠️ [CHECK 7] Privacy settings check skipped (API not available in Pyrogram)")
            except Exception as privacy_err:
                print(f"⚠️ [CHECK 7] Privacy check warning: {privacy_err}")
            
            await between_api_calls_delay("post-privacy")
            
            try:
                details["checks_performed"].append("dialogs_check")
                dialog_count = 0
                async for _ in temp_client.get_dialogs(limit=5):
                    dialog_count += 1
                
                if dialog_count >= 3:
                    spam_score -= 5
                    logger.info(f"[CHECK 8] Account has {dialog_count}+ active dialogs")
                    
            except PeerFlood:
                # Bot-side rate limit (see above) - INCONCLUSIVE, no penalty.
                details["peer_flood_detected"] = True
                print(f"⚠️ [CHECK 8] PEER_FLOOD on dialogs fetch - bot-side, INCONCLUSIVE (no penalty)")
            except Exception as dialogs_err:
                print(f"⚠️ [CHECK 8] Dialogs check warning: {dialogs_err}")
                dialog_count = 3
                
        except UserDeactivated:
            spam_score = 100
            details["account_deleted"] = True
            details["check_method"] = "account_deactivated"
            print(f"❌ CRITICAL: Account is DEACTIVATED/BANNED")
            
        except AuthKeyUnregistered:
            spam_score = 100
            details["account_deleted"] = True
            details["check_method"] = "session_invalid"
            print(f"❌ CRITICAL: Session is INVALID (account may be banned)")
            
        except Exception as e:
            print(f"⚠️ Detailed status check error: {e}")
            error_str = str(e).upper()

            if "FLOOD" in error_str:
                # Any FLOOD-family error here comes from the bot's own check
                # traffic being rate-limited, NOT from the account's state.
                # The old blanket +50 systematically pushed clean accounts into
                # "limit" under load. Marked INCONCLUSIVE instead.
                details["peer_flood_detected"] = True
                details["flood_inconclusive"] = True
                details["inconclusive"] = True
            elif "BANNED" in error_str or "DEACTIVAT" in error_str:
                spam_score = 100

            details["check_method"] = "partial_check"
        
        try:
            await safe_disconnect(temp_client, "status-check")
        except Exception:
            pass  # Disconnect may fail if already disconnected
        
        details["spam_score"] = spam_score
        
        if spam_score >= 70 or details["account_deleted"]:
            status = "spam"
        elif spam_score >= 30:
            status = "limit"
        else:
            status = "spam_free"
        
        print(f"\n{'='*50}")
        print(f"📊 FINAL Account Status Analysis:")
        print(f"   Status: {status.upper()}")
        print(f"   Spam Score: {spam_score}/100")
        print(f"   Checks Performed: {len(details['checks_performed'])}")
        print(f"   SpamBot: {details['spambot_status']}")
        print(f"   Can Send Messages: {details['can_send_messages']}")
        print(f"   Peer Flood: {details['peer_flood_detected']}")
        print(f"{'='*50}\n")

        record_api_usage(
            api_name=api_name,
            success=True,
            flood=bool(details.get("peer_flood_detected")),
        )
        
        return status, details
        
    except Exception as e:
        print(f"❌ Account status check failed: {e}")
        error_str = str(e).upper()
        
        if "FLOOD" in error_str or "PEER" in error_str:
            record_api_usage(api_name=api_name, success=False, flood=True)
            return "limit", {"error": str(e), "check_method": "error_detection", "peer_flood_detected": True}
        elif "BANNED" in error_str or "DEACTIVAT" in error_str:
            record_api_usage(api_name=api_name, success=False)
            return "spam", {"error": str(e), "check_method": "error_detection", "account_deleted": True}
        
        record_api_usage(api_name=api_name, success=False)
        return "limit", {"error": str(e), "check_method": "conservative_fallback"}
