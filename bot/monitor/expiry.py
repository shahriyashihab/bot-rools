"""Multi-device window expiry is handled by the standard REJECTED outcome.

The separate EXPIRED outcome for multi-device sessions was removed: the 24h
monitor flow now resolves every session to SUCCESS / MULTI-DEVICE / REJECTED
and failures route to the standard rejection path. This module is kept as a
no-op stub for backward compatibility with old imports.
"""
