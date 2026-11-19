"""Startup and shutdown hooks for the overload fixes (flag-scoped; no-ops with every flag off)."""

from __future__ import annotations

from . import flags
from .logging_config import logger
from .overload_config import SHUTDOWN_GRACE_S


async def startup_fixes() -> None:
    """Recover safety state left by a previous process (1.8)."""
    if flags.enabled("SEND_LEDGER"):
        from .services.safety import ledger

        swept = ledger.get_ledger().sweep_stale_pending(older_than_s=0)  # process start: nothing can be in flight
        if swept:
            logger.warning("Marked %d in-flight email sends from a previous process as 'unknown'", swept)


async def shutdown_fixes() -> None:
    """Let in-flight sends finish (bounded), stop tracked background work, then stop the Gmail pool."""
    grace = float(SHUTDOWN_GRACE_S.get())
    if flags.enabled("GMAIL_SENDS_OFF_LOOP"):
        from .services.safety import send_guard

        still_running = await send_guard.wait_for_in_flight(grace)
        if still_running:
            logger.warning("%d email sends still in flight at shutdown; they stay 'pending' and become 'unknown' on restart", still_running)
    from .utils import tasks

    cancelled = await tasks.shutdown(grace)
    if cancelled:
        logger.warning("Cancelled %d background tasks at shutdown", cancelled)
    from .services.gmail.executor import shutdown_gmail_executor

    shutdown_gmail_executor(wait=False)
