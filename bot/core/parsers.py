"""Verification result parsing utilities."""
from typing import Optional, Tuple, Any


def parse_verification_result(result: Any) -> Tuple[bool, str, Optional[str]]:
    """
    Safely parse verification result tuple.
    Handles tuples of varying lengths returned by verification functions.

    Args:
        result: The result from verification functions (tuple or other)

    Returns:
        Tuple of (success: bool, message: str, session_id: Optional[str])
    """
    if isinstance(result, tuple):
        success = result[0] if len(result) > 0 else False
        msg = result[1] if len(result) > 1 else "Error occurred"
        session_id = result[2] if len(result) > 2 else None
        return success, msg, session_id
    return False, str(result) if result else "Error occurred", None
