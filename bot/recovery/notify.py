# bot/recovery/notify.py
"""DEAD MODULE - intentionally neutralized.

Per the delivery rule the user NEVER receives a "recovery in progress"
message: recovery is silent and the user only ever gets one of the three
genuine verdict messages (SUCCESS / MULTI-DEVICE / REJECTED) once their
session resolves. The orchestrator (bot/recovery/run.py) no longer calls
this module; the function body is kept as a no-op so any stale import
(bot/recovery/__init__.py) still succeeds without ever messaging a user.
"""
import asyncio
from typing import Dict, List, Tuple

from pyrogram.client import Client


async def notify_users_recovery_starting(
    bot_client: Client,
    recovery_list: List[Tuple[str, Dict]]
) -> Dict[int, bool]:
    """No-op. Recovery must be silent for the user (delivery rule).

    Kept for import compatibility only - DO NOT re-add user messaging here.
    """
    return {}
