"""Background watcher that surfaces important Gmail emails proactively."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ... import flags
from .client import execute_gmail_tool, get_active_gmail_user_id
from .executor import run_gmail
from .processing import EmailTextCleaner, ProcessedEmail, parse_gmail_fetch_response
from .seen_store import GmailSeenStore
from .importance_classifier import classify_email_importance, classify_email_importance_detailed
from ...logging_config import logger
from ...utils.timezones import convert_to_user_timezone
from ...overload_config import (
    WATCHER_BUFFER_MAX,
    WATCHER_CLASSIFY_MAX_ATTEMPTS,
    WATCHER_SHUTDOWN_GRACE_S,
    WATCHER_TURN_REQUEUE_MAX,
)
from ...utils import tasks
from .. import telemetry
from ..context import new_id, turn_scope


if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...agents.interaction_agent.runtime import InteractionAgentRuntime


def _resolve_interaction_runtime() -> "InteractionAgentRuntime":
    from ...agents.interaction_agent.runtime import InteractionAgentRuntime

    return InteractionAgentRuntime()


DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_LOOKBACK_MINUTES = 10
DEFAULT_MAX_RESULTS = 50
DEFAULT_SEEN_LIMIT = 300


from ...paths import data_dir

_DATA_DIR = data_dir()  # OPENPOKE_DATA_DIR override; default unchanged (server/data)
_DEFAULT_SEEN_PATH = _DATA_DIR / "gmail_seen.json"


class ImportantEmailWatcher:
    """Poll Gmail for recent messages and surface important ones."""

    def __init__(
        self,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
        *,
        seen_store: Optional[GmailSeenStore] = None,
    ) -> None:
        self._poll_interval = poll_interval_seconds
        self._lookback_minutes = lookback_minutes
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._seen_store = seen_store or GmailSeenStore(_DEFAULT_SEEN_PATH, DEFAULT_SEEN_LIMIT)
        self._cleaner = EmailTextCleaner(max_url_length=60)
        self._has_seeded_initial_snapshot = False
        self._last_poll_timestamp: Optional[datetime] = None
        # WATCHER_SINGLE_FLIGHT (2.8) state.
        self._pending: List[Tuple[str, int]] = []  # (summary, times re-enqueued after a failed turn)
        self._overflow = 0
        self._turn_task: Optional[asyncio.Task] = None
        self._classify_attempts: Dict[str, int] = {}
        self._accepting = True
        self._last_poll_id: Optional[str] = None

    # Start the background email polling task
    async def start(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                return
            loop = asyncio.get_running_loop()
            self._running = True
            self._has_seeded_initial_snapshot = False
            self._last_poll_timestamp = None
            self._task = loop.create_task(self._run(), name="important-email-watcher")
            logger.info(
                "Important email watcher started",
                extra={"interval_seconds": self._poll_interval, "lookback_minutes": self._lookback_minutes},
            )

    # Stop the background email polling task gracefully
    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._task = None
                logger.info("Important email watcher stopped")
            if flags.enabled("WATCHER_SINGLE_FLIGHT"):
                await self._stop_single_flight()

    async def _run(self) -> None:
        try:
            while self._running:
                try:
                    await self._poll_once()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("Important email watcher poll failed", extra={"error": str(exc)})
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            raise

    # Poll Gmail once for new messages and classify them for importance
    def _complete_poll(self, user_now: datetime) -> None:
        self._last_poll_timestamp = user_now
        self._has_seeded_initial_snapshot = True

    async def _poll_once(self) -> None:
        poll_started_at = datetime.now(timezone.utc)
        user_now = convert_to_user_timezone(poll_started_at)
        first_poll = not self._has_seeded_initial_snapshot
        previous_poll_timestamp = self._last_poll_timestamp
        interval_cutoff = user_now - timedelta(seconds=self._poll_interval)
        cutoff_time = interval_cutoff
        if previous_poll_timestamp is not None and previous_poll_timestamp > interval_cutoff:
            cutoff_time = previous_poll_timestamp

        composio_user_id = get_active_gmail_user_id()
        if not composio_user_id:
            logger.debug("Gmail not connected; skipping importance poll")
            return

        query = f"label:INBOX newer_than:{self._lookback_minutes}m"
        arguments = {
            "query": query,
            "include_payload": True,
            "max_results": DEFAULT_MAX_RESULTS,
        }

        try:
            if flags.enabled("GMAIL_READS_OFF_LOOP"):
                raw_result = await run_gmail(execute_gmail_tool, "GMAIL_FETCH_EMAILS", composio_user_id, arguments=arguments)
            else:
                raw_result = execute_gmail_tool("GMAIL_FETCH_EMAILS", composio_user_id, arguments=arguments)
        except Exception as exc:
            logger.warning(
                "Failed to fetch Gmail messages for watcher",
                extra={"error": str(exc)},
            )
            return

        processed_emails, _ = parse_gmail_fetch_response(
            raw_result,
            query=query,
            cleaner=self._cleaner,
        )

        if not processed_emails:
            logger.debug("No recent Gmail messages found for watcher")
            self._complete_poll(user_now)
            return

        if first_poll:
            self._seen_store.mark_seen(email.id for email in processed_emails)
            logger.info(
                "Important email watcher completed initial warmup",
                extra={"skipped_ids": len(processed_emails)},
            )
            self._complete_poll(user_now)
            return

        unseen_emails: List[ProcessedEmail] = [
            email for email in processed_emails if not self._seen_store.is_seen(email.id)
        ]

        if not unseen_emails:
            logger.info(
                "Important email watcher check complete",
                extra={"emails_reviewed": 0, "surfaced": 0},
            )
            self._complete_poll(user_now)
            return

        unseen_emails.sort(key=lambda email: email.timestamp or datetime.now(timezone.utc))

        if flags.enabled("WATCHER_SINGLE_FLIGHT"):
            await self._process_single_flight(unseen_emails, cutoff_time, user_now)
            return

        eligible_emails: List[ProcessedEmail] = []
        aged_emails: List[ProcessedEmail] = []

        for email in unseen_emails:
            email_timestamp = email.timestamp
            if email_timestamp.tzinfo is not None:
                email_timestamp = email_timestamp.astimezone(user_now.tzinfo)
            else:
                email_timestamp = email_timestamp.replace(tzinfo=user_now.tzinfo)

            if email_timestamp < cutoff_time:
                aged_emails.append(email)
                continue

            eligible_emails.append(email)

        if not eligible_emails and aged_emails:
            self._seen_store.mark_seen(email.id for email in aged_emails)
            logger.info(
                "Important email watcher check complete",
                extra={
                    "emails_reviewed": len(unseen_emails),
                    "surfaced": 0,
                    "suppressed_for_age": len(aged_emails),
                },
            )
            self._complete_poll(user_now)
            return

        summaries_sent = 0
        processed_ids: List[str] = [email.id for email in aged_emails]
        poll_id = new_id("poll")

        for email in eligible_emails:
            summary = await classify_email_importance(email)
            processed_ids.append(email.id)
            if not summary:
                continue

            summaries_sent += 1
            await self._dispatch_summary(summary, poll_id)

        telemetry.emit(
            "poll",
            kind="watcher",
            new=len(unseen_emails),
            eligible=len(eligible_emails),
            turns_started=summaries_sent,
            buffered=0,
            dropped=0,
            classify_errors=None,
            poll_id=poll_id,
        )

        if processed_ids:
            self._seen_store.mark_seen(processed_ids)

        logger.info(
            "Important email watcher check complete",
            extra={
                "emails_reviewed": len(unseen_emails),
                "surfaced": summaries_sent,
                "suppressed_for_age": len(aged_emails),
            },
        )
        self._complete_poll(user_now)

    async def _dispatch_summary(self, summary: str, poll_id: Optional[str] = None) -> None:
        runtime = _resolve_interaction_runtime()
        try:
            contextualized = f"Important email watcher notification:\n{summary}"
            with turn_scope("email", poll_id=poll_id):
                await runtime.handle_agent_message(contextualized, origin="email")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Failed to dispatch important email summary",
                extra={"error": str(exc)},
            )


    # ------------------------------------------------------------------ 2.8 single flight
    async def _process_single_flight(self, unseen: List[ProcessedEmail], cutoff_time: datetime, user_now: datetime) -> None:
        """Classify, buffer (bounded) and hand off to at most ONE in-flight IA turn (I6).

        The poll never awaits the IA turn, so the 60 s cadence holds under bursts.
        Classification errors are retried on later polls (never silently "seen"),
        then dropped with a warning event after WATCHER_CLASSIFY_MAX_ATTEMPTS.
        """
        poll_id = new_id("poll")
        self._last_poll_id = poll_id
        eligible: List[ProcessedEmail] = []
        seen: List[str] = []
        for email in unseen:
            stamp = email.timestamp.astimezone(user_now.tzinfo) if email.timestamp.tzinfo else email.timestamp.replace(tzinfo=user_now.tzinfo)
            if stamp < cutoff_time and email.id not in self._classify_attempts:
                seen.append(email.id)  # aged out (unchanged behaviour), unless it is waiting for a retry
            else:
                eligible.append(email)

        errors = dropped = overflowed = surfaced = 0
        max_attempts = int(WATCHER_CLASSIFY_MAX_ATTEMPTS.get())
        for email in eligible:
            summary, error = await classify_email_importance_detailed(email)
            if error is not None:
                errors += 1
                attempts = self._classify_attempts.get(email.id, 0) + 1
                if attempts >= max_attempts:
                    self._classify_attempts.pop(email.id, None)
                    seen.append(email.id)
                    dropped += 1
                    logger.warning("Dropping email %s after %d failed importance classifications", email.id, attempts)
                    telemetry.emit("watcher_email_dropped", message_id_sha=telemetry.sha(email.id), attempts=attempts, reason=error)
                else:
                    self._classify_attempts[email.id] = attempts  # not seen: retried on the next poll
                continue
            self._classify_attempts.pop(email.id, None)
            if summary:
                surfaced += 1
                if not self._enqueue(summary, 0):
                    overflowed += 1
            seen.append(email.id)  # important: buffered first, then marked seen

        if seen:
            self._seen_store.mark_seen(seen)
        started = self._maybe_start_turn(poll_id)
        telemetry.emit("poll", kind="watcher", new=len(unseen), eligible=len(eligible), turns_started=1 if started else 0,
                       buffered=len(self._pending), dropped=overflowed, classify_errors=errors,
                       classify_dropped=dropped, poll_id=poll_id)
        logger.info("Important email watcher check complete",
                    extra={"emails_reviewed": len(unseen), "surfaced": surfaced, "buffered": len(self._pending)})
        self._complete_poll(user_now)

    def _enqueue(self, summary: str, requeues: int, *, front: bool = False) -> bool:
        if len(self._pending) >= int(WATCHER_BUFFER_MAX.get()):
            self._overflow += 1  # counted and reported in the next turn
            return False
        if front:
            self._pending.insert(0, (summary, requeues))
        else:
            self._pending.append((summary, requeues))
        return True

    def _maybe_start_turn(self, poll_id: Optional[str]) -> bool:
        if not self._pending or not self._accepting:
            return False
        if self._turn_task is not None and not self._turn_task.done():
            return False  # single flight: the buffer waits for the current turn
        batch, self._pending = self._pending, []
        overflow, self._overflow = self._overflow, 0
        lines = ["Important email watcher notification:"] + [f"- {summary}" for summary, _ in batch]
        if overflow:
            lines.append(f"...and {overflow} more important emails not shown")
        task = tasks.spawn(self._run_turn("\n".join(lines), poll_id), name="watcher-turn")
        task.add_done_callback(lambda t, b=batch: self._turn_done(t, b))
        self._turn_task = task
        return True

    async def _run_turn(self, message: str, poll_id: Optional[str]) -> bool:
        runtime = _resolve_interaction_runtime()
        with turn_scope("email", poll_id=poll_id):
            result = await runtime.handle_agent_message(message, origin="email")
        return bool(result.success)

    def _turn_done(self, task: asyncio.Task, batch: List[Tuple[str, int]]) -> None:
        if task is self._turn_task:
            self._turn_task = None
        if task.cancelled():
            return  # shutdown: counted as lost by _stop_single_flight
        failed = task.exception() is not None or task.result() is False
        if failed:
            limit = int(WATCHER_TURN_REQUEUE_MAX.get())
            requeue = [(s, n + 1) for s, n in batch if n < limit]
            lost = len(batch) - len(requeue)
            logger.error("Important email watcher turn failed; re-enqueued %d, dropped %d", len(requeue), lost)
            telemetry.emit("watcher_turn_failed", requeued=len(requeue), dropped=lost)
            for summary, requeues in reversed(requeue):
                self._enqueue(summary, requeues, front=True)
        self._maybe_start_turn(self._last_poll_id)

    async def _stop_single_flight(self) -> None:
        self._accepting = False
        task = self._turn_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=float(WATCHER_SHUTDOWN_GRACE_S.get()))
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:  # pragma: no cover - the done-callback already reported it
                pass
        lost = len(self._pending) + self._overflow
        if lost:
            logger.warning("Important email watcher stopped with %d buffered summaries not delivered", lost)
        telemetry.emit("watcher_shutdown", lost=lost)
        self._pending, self._overflow = [], 0


_watcher_instance: Optional[ImportantEmailWatcher] = None


def get_important_email_watcher() -> ImportantEmailWatcher:
    global _watcher_instance
    if _watcher_instance is None:
        _watcher_instance = ImportantEmailWatcher()
    return _watcher_instance


__all__ = ["ImportantEmailWatcher", "get_important_email_watcher"]
