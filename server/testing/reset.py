"""Reset every store and in-memory singleton between tests (test-only helper).

Stores are cleared through their existing public methods. In-memory state
(batches, scheduler bookkeeping, summarizer flags, admission state, loop-bound
asyncio primitives) is rebuilt, because each test runs on a fresh event loop
and an asyncio.Lock bound to a closed loop would fail.

Refuses to run against the default data directory: tests must set
OPENPOKE_DATA_DIR before importing ``server`` (see tests/conftest.py).
"""

from __future__ import annotations

import asyncio
import os

from ..paths import DATA_DIR_ENV, data_dir, default_data_dir


def reset_state() -> None:
    if not os.environ.get(DATA_DIR_ENV) or data_dir() == default_data_dir():
        raise RuntimeError("reset_state() refuses to touch the real data directory; set OPENPOKE_DATA_DIR")

    from ..agents.execution_agent import batch_manager
    from ..services.conversation import get_conversation_log
    from ..services.conversation.summarization import scheduler as summarization_scheduler
    from ..services.execution import get_agent_roster, get_execution_agent_logs
    from ..services.gmail import client as gmail_client
    from ..services.gmail import importance_watcher
    from ..services import telemetry, trigger_scheduler
    from ..services.timezone_store import get_timezone_store
    from ..services.triggers import get_trigger_service
    from ..utils import clock, tasks
    from . import fake_gmail

    # Persistent stores (public clear methods).
    get_conversation_log().clear()
    get_execution_agent_logs().clear_all()
    get_agent_roster().clear()
    get_trigger_service().clear_all()
    get_timezone_store().clear()

    # Batch manager (process-wide instance, also aliased in interaction_agent.tools).
    batch_manager.get_batch_manager().reset_for_tests()

    # Scheduler and watcher singletons: drop them so the next access builds fresh ones.
    trigger_scheduler._scheduler_instance = None
    watcher = importance_watcher._watcher_instance
    if watcher is not None:
        watcher._seen_store.clear()
    importance_watcher._watcher_instance = None

    # Summarizer worker flags.
    summarization_scheduler._pending = False
    summarization_scheduler._running = False

    # Gmail client in-memory state.
    gmail_client._set_active_gmail_user_id(None)
    gmail_client._clear_cached_profile()

    # Fixes' process-global state (modules may not exist yet in early phases).
    for module_name in (
        "server.agents.execution_agent.admission",
        "server.services.gmail.executor",
        "server.services.safety.ledger",
        "server.services.safety.trigger_claims",
        "server.services.safety.approvals",
        "server.services.safety.send_guard",
        "server.services.agents.registry",
        "server.services.agents.resolver",
    ):
        module = _optional_import(module_name)
        reset = getattr(module, "reset_for_tests", None) if module else None
        if reset is not None:
            reset()

    fake_gmail.install_fake_gmail(None)
    tasks.reset()
    clock.reset()
    telemetry.reset()

    # Leftover telemetry files from the previous test.
    events = telemetry.events_path()
    for path in (events, events.with_suffix(events.suffix + ".1")):
        if path.exists():
            path.unlink()


def _optional_import(name: str):
    import importlib

    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return None


def ensure_no_running_loop() -> None:
    """Guard: reset must not run inside an event loop (it rebuilds loop-bound locks)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("reset_state() must be called outside a running event loop")


__all__ = ["ensure_no_running_loop", "reset_state"]
