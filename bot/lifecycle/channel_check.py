"""Channel access + post-permission checks at startup."""
import logging
import os

from bot import config
from bot.bootstrap.client import app

logger = logging.getLogger(__name__)


async def resolve_channels():
    """Resolve all channels to cache peer information."""
    channels = [
        config.get_channel("CHANNEL_SUBMIT"),
        config.get_channel("CHANNEL_VERIFIED"),
        config.get_channel("CHANNEL_REJECTED"),
        config.get_channel("CHANNEL_WITHDRAW_REPORT"),
        config.get_channel("CHANNEL_24HR_UP")
    ]

    for channel_id in channels:
        if not channel_id:
            continue
        try:
            chat = await app.get_chat(channel_id)
            print(f"✅ Channel resolved: {chat.title} ({channel_id})")
        except Exception as e:
            logger.warning(f"⚠️ Failed to resolve channel {channel_id}: {e}")


async def check_all_channels():
    """Check all channel access AND posting permission at startup."""
    print("\n" + "="*60)
    print("🔍 Checking channel access and post permissions...")
    print("="*60)

    channels = {
        "📥 CHANNEL_SUBMIT (Pending)": config.get_channel("CHANNEL_SUBMIT"),
        "✅ CHANNEL_VERIFIED": config.get_channel("CHANNEL_VERIFIED"),
        "❌ CHANNEL_REJECTED": config.get_channel("CHANNEL_REJECTED"),
        "💸 CHANNEL_WITHDRAW_REPORT": config.get_channel("CHANNEL_WITHDRAW_REPORT"),
        "⏰ CHANNEL_24HR_UP": config.get_channel("CHANNEL_24HR_UP")
    }

    accessible = 0
    not_accessible = 0
    no_post_permission = 0

    for name, channel_id in channels.items():
        if not channel_id:
            logger.warning(f"⚠️ {name}: not configured, skipping")
            continue
        try:
            chat = await app.get_chat(channel_id)

            # P5-48: the old startup check POSTED A REAL MESSAGE into every
            # channel on EVERY restart (spam + FloodWait + a visible "test"
            # artifact in the buyer-facing channels). The permission is now
            # read via get_chat_member instead: can_post_messages is a
            # reliable, side-effect-free answer. A real send-and-delete probe
            # runs ONLY when the member info is unavailable (rare legacy
            # setups) or explicitly requested via CHANNEL_TEST_MESSAGE=1.
            post_ok = False
            member_error = None
            try:
                me_bot = await app.get_me()
                member = await app.get_chat_member(channel_id, me_bot.id)
                can_post = bool(
                    getattr(member, "can_post_messages", False)
                    or getattr(getattr(member, "privileges", None), "can_post_messages", False)
                )
                # Anonymous admins surface as status="administrator" without
                # explicit flags - treat administrator status as post-capable.
                if not can_post and str(getattr(member, "status", "")).lower() == "administrator":
                    can_post = True
                post_ok = can_post
            except Exception as member_err:
                member_error = member_err

            if not post_ok and member_error is not None and os.environ.get("CHANNEL_TEST_MESSAGE") == "1":
                try:
                    test_msg = await app.send_message(
                        chat_id=channel_id,
                        text="🔧 Bot permission test - auto delete"
                    )
                    await test_msg.delete()
                    post_ok = True
                except Exception:
                    post_ok = False

            print(f"✅ {name}")
            print(f"   📺 Title: {chat.title}")
            print(f"   🆔 ID: {channel_id}")
            if post_ok:
                print("   ✍️ Post Permission: ✅ YES")
                accessible += 1
            else:
                print("   ✍️ Post Permission: ❌ NO (check via get_chat_member)")
                logger.warning(f"⚠️ {name}: no post permission detected{f' ({member_error})' if member_error else ''}")
                print("   💡 Fix: Make the bot Admin with 'Post Messages' permission")
                no_post_permission += 1
                not_accessible += 1

        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ {name}")
            print(f"   🆔 ID: {channel_id}")
            logger.warning(f"   ⚠️ Error: {error_msg[:60]}")
            print("   💡 Fix: Add the bot as admin to this channel")
            not_accessible += 1

    print("="*60)
    print("📊 Channel Status Summary:")
    print(f"   ✅ Full Access (Read + Post): {accessible}/{len(channels)}")
    logger.warning(f"   ⚠️ No Post Permission: {no_post_permission}/{len(channels)}")
    logger.error(f"   ❌ No Access: {not_accessible - no_post_permission}/{len(channels)}")

    if not_accessible > 0:
        print("\n" + "="*60)
        logger.warning("⚠️ Some channels have problems!")
        print("="*60)
        print("📌 Solutions:")
        print("   1. Go to each channel")
        print("   2. Add the bot as Admin")
        print("   3. Grant these permissions:")
        print("      ✅ Post Messages")
        print("      ✅ Edit Messages (optional)")
        print("      ✅ Delete Messages (optional)")
        print("   4. Use /test_channels to check again")
        print("="*60)
    else:
        print("\n✅ All channels have FULL ACCESS! Reports will be sent correctly.")

    print("="*60 + "\n")

    return accessible, not_accessible
