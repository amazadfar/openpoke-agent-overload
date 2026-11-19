"""Wall-clock and monotonic time with a test/eval override (single owner).

Production code that makes *policy* decisions on time (dormancy, approval
expiry, dedupe windows, near-duplicate windows, trigger due-ness) reads time
here so tests and the eval runner can advance it deterministically. With no
offset set, these are exactly ``datetime.now(UTC)`` and ``time.monotonic()``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

_lock = threading.Lock()
_offset_s = 0.0


def now() -> datetime:
    """Timezone-aware UTC now, shifted by the test offset."""
    with _lock:
        offset = _offset_s
    current = datetime.now(UTC)
    return current + timedelta(seconds=offset) if offset else current


def monotonic() -> float:
    """time.monotonic(), shifted by the test offset."""
    with _lock:
        offset = _offset_s
    return time.monotonic() + offset


def advance(seconds: float) -> None:
    """Move both clocks forward (tests and evals only)."""
    global _offset_s
    with _lock:
        _offset_s += float(seconds)


def offset() -> float:
    with _lock:
        return _offset_s


def reset() -> None:
    """Remove any offset (tests only)."""
    global _offset_s
    with _lock:
        _offset_s = 0.0


__all__ = ["UTC", "advance", "monotonic", "now", "offset", "reset"]
