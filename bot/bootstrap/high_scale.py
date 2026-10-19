"""High-scale system initialization and shutdown routines."""
import asyncio
import logging

logger = logging.getLogger(__name__)

_high_scale_initialized = False


async def initialize_high_scale_systems():
    """Initialize all high-scale systems (Redis, Memory Manager, etc.)"""
    global _high_scale_initialized
    if _high_scale_initialized:
        return

    print("🚀 Initializing high-scale systems...")

    try:
        from bot.utils import redis_cache
        redis_ok = await redis_cache.init_redis()
        if redis_ok:
            print("✅ Redis cache initialized")
            from bot.middleware.channel_guard import init_redis_cache
            await init_redis_cache()
        else:
            logger.warning("⚠️ Redis not available, using in-memory fallback")
    except Exception as e:
        logger.warning(f"⚠️ Redis init error (non-fatal): {e}")

    try:
        from bot.utils import high_scale_storage
        await high_scale_storage.init_high_scale_storage()
        print("✅ High-scale storage initialized")
    except Exception as e:
        logger.warning(f"⚠️ High-scale storage init error (non-fatal): {e}")

    try:
        from bot.utils import memory_manager
        await memory_manager.start_memory_manager()
        print("✅ Memory manager started")
    except Exception as e:
        logger.warning(f"⚠️ Memory manager init error (non-fatal): {e}")

    _high_scale_initialized = True
    print("🎉 High-scale systems ready!")


async def shutdown_high_scale_systems():
    """Gracefully shutdown high-scale systems."""
    print("🔄 Shutting down high-scale systems...")

    try:
        from bot.utils import memory_manager
        await memory_manager.stop_memory_manager()
    except Exception:
        pass

    try:
        from bot.utils import high_scale_storage
        await high_scale_storage.close_high_scale_storage()
    except Exception:
        pass

    try:
        from bot.utils import redis_cache
        await redis_cache.close_redis()
    except Exception:
        pass

    print("✅ High-scale systems shutdown complete")
