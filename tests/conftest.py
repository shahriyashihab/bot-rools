"""Pytest bootstrap - MUST run before any ``bot`` import.

``bot.data.state`` resolves its SQLite path from the environment at import
time and calls ``load_storage()`` on import. Without the env redirect below,
importing the bot package from a test would touch the PRODUCTION database
(``data/bot_data.db``). Every test therefore runs against a throwaway
database in a temp directory, and dummy credentials guarantee ``bot.config``
never depends on a real token.
"""

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="bot_tests_")
os.environ.setdefault("SQLITE_DB_PATH", os.path.join(_TMP, "test_bot_data.db"))
os.environ.setdefault("STORAGE_FALLBACK_JSON", os.path.join(_TMP, "test_fallback.json"))
os.environ.setdefault("BOT_TOKEN", "123456789:TEST_TOKEN_FOR_PYTEST")
os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("ADMIN_ID", "1")

# Make the project root importable (pytest rootdir insertion depends on the
# invocation directory; this makes ``python -m pytest`` and IDE runs equal).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
