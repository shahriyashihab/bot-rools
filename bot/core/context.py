"""Shared application context (event loop state, background tasks)."""
import asyncio

shutdown_event = asyncio.Event()
cleanup_tasks = []
