"""Bot entry point - slim module that wires all packages together.

Kept at ``bot/main.py`` so ``python3 bot/main.py`` (start.sh / .replit) keeps
working. All logic now lives in the ``bot/`` packages:

- bot.bootstrap       → client, guard, registration, high-scale systems
- bot.handlers        → message/callback/channel/text handlers
- bot.monitor         → pending-session monitor (orchestrator + phases)
- bot.lifecycle       → startup, shutdown, channel checks, cleanup
- bot.data            → storage persistence (JSON/SQLite)
- bot.services        → channel sender, notifications, session file I/O
"""
import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Import every handler module (triggers decorator registration).
# The channel guard is applied automatically by bot.bootstrap.client.
from bot.bootstrap.registration import *  # noqa: F401,F403

from bot.__main__ import main

if __name__ == "__main__":
    main()
