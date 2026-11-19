"""Background scheduler that watches trigger definitions and executes them."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Set

from .. import flags
from ..agents.execution_agent.batch_manager import ExecutionBatchManager, Ticket, get_batch_manager
from ..agents.execution_agent.runtime import ExecutionResult
from ..logging_config import logger
from ..utils import clock, tasks
from . import telemetry
from .context import new_id, turn_scope
from .triggers import TriggerRecord, get_trigger_service
from .triggers.utils import parse_iso


UTC = timezone.utc


def _utc_now() -> datetime:
    return clock.now()  # == datetime.now(UTC) unless a test/eval advanced the clock


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class _Occurrence:
    fired_at: datetime
    instructions: str
    scheduled_for: str
    run_id: str


class TriggerScheduler:
    """Polls stored triggers and launches execution agents when due."""

    def __init__(self, poll_interval_seconds: float = 10.0) -> None:
        self._poll_interval = poll_interval_seconds
        self._service = get_trigger_service()
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._in_flight: Set[int] = set()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                return
            if flags.enabled("SEND_LEDGER"):
                self._recover_abandoned_occurrences()
            loop = asyncio.get_running_loop()
            self._running = True
            self._task = loop.create_task(self._run(), name="trigger-scheduler")
            logger.info("Trigger scheduler started", extra={"interval": self._poll_interval})

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None
                logger.info("Trigger scheduler stopped")

    async def _run(self) -> None:
        try:
            while self._running:
                await self._poll_once()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Trigger scheduler loop crashed", extra={"error": str(exc)})

    async def _poll_once(self) -> None:
        if flags.enabled("SEND_LEDGER"):
            self._sweep_stale_sends()
        now = _utc_now()
        due_triggers = self._service.get_due_triggers(before=now)
        if not due_triggers:
            return

        poll_id = new_id("poll")
        fresh = [trigger for trigger in due_triggers if trigger.id not in self._in_flight]
        for trigger in fresh:
            self._in_flight.add(trigger.id)
        if flags.enabled("POLL_BATCH"):
            turns_started = await self._start_poll_batch(fresh, poll_id)
        else:
            for trigger in fresh:
                asyncio.create_task(self._execute_trigger(trigger, poll_id), name=f"trigger-{trigger.id}")
            turns_started = len(fresh)
        telemetry.emit("poll", kind="trigger", due=len(due_triggers), turns_started=turns_started, poll_id=poll_id)

    async def _start_poll_batch(self, triggers: List[TriggerRecord], poll_id: str) -> int:
        """2.7 (M4): one turn for the whole poll. Every run is registered in the poll's
        batch BEFORE any starts, so the batch cannot drain early: one IA turn per poll."""
        manager = get_batch_manager()
        prepared = []
        with turn_scope("trigger", poll_id=poll_id):
            for trigger in triggers:
                occurrence = self._prepare_occurrence(trigger)
                if occurrence is None:
                    continue
                ticket = await manager.register(trigger.agent_name, occurrence.instructions, occurrence.run_id)
                prepared.append((trigger, occurrence, ticket))
            for trigger, occurrence, ticket in prepared:
                tasks.spawn(self._run_occurrence(trigger, occurrence, manager, ticket), name=f"trigger-{trigger.id}")
        return 1 if prepared else 0

    async def _execute_trigger(self, trigger: TriggerRecord, poll_id: Optional[str] = None) -> None:
        # Each due trigger is its own root turn (origin=trigger) unless POLL_BATCH.
        with turn_scope("trigger", poll_id=poll_id):
            if flags.enabled("SEND_LEDGER"):
                occurrence = self._prepare_occurrence(trigger)
                if occurrence is not None:
                    await self._run_occurrence(trigger, occurrence, ExecutionBatchManager(), None)
                return
            await self._execute_trigger_in_turn(trigger)

    async def _execute_trigger_in_turn(self, trigger: TriggerRecord) -> None:
        try:
            fired_at = _utc_now()
            instructions = self._format_instructions(trigger, fired_at)
            logger.info(
                "Dispatching trigger",
                extra={
                    "trigger_id": trigger.id,
                    "agent": trigger.agent_name,
                    "scheduled_for": trigger.next_trigger,
                },
            )
            execution_manager = ExecutionBatchManager()
            result = await execution_manager.execute_agent(
                trigger.agent_name,
                instructions,
            )
            if result.success:
                self._handle_success(trigger, fired_at)
            else:
                error_text = result.error or result.response
                self._handle_failure(trigger, fired_at, error_text)
        except Exception as exc:  # pragma: no cover - defensive
            self._handle_failure(trigger, _utc_now(), str(exc))
            logger.exception(
                "Trigger execution failed unexpectedly",
                extra={"trigger_id": trigger.id, "agent": trigger.agent_name},
            )
        finally:
            self._in_flight.discard(trigger.id)

    # ------------------------------------------------------------ 1.8 claims
    def _prepare_occurrence(self, trigger: TriggerRecord) -> Optional["_Occurrence"]:
        """Claim the occurrence durably before running it (SEND_LEDGER); None = do not run."""
        fired_at = _utc_now()
        occurrence = _Occurrence(
            fired_at=fired_at,
            instructions=self._format_instructions(trigger, fired_at),
            scheduled_for=trigger.next_trigger or _isoformat(fired_at),
            run_id=str(uuid.uuid4()),
        )
        if not flags.enabled("SEND_LEDGER"):
            return occurrence
        from .safety.trigger_claims import get_trigger_claims

        claims = get_trigger_claims()
        if claims.claim(trigger.id, occurrence.scheduled_for, occurrence.run_id):
            return occurrence
        existing = claims.get(trigger.id, occurrence.scheduled_for)
        telemetry.emit("trigger_claim_conflict", trigger_id=trigger.id, claim_status=existing.status if existing else None)
        if existing is not None and existing.status != "claimed":
            # Finished or abandoned but still due: advance it instead of re-running it.
            self._advance_without_running(trigger.id, occurrence.scheduled_for)
        self._in_flight.discard(trigger.id)
        return None

    async def _run_occurrence(self, trigger: TriggerRecord, occurrence: "_Occurrence", manager: ExecutionBatchManager,
                              ticket: Optional[Ticket]) -> None:
        status = "failed"
        cancelled = False
        try:
            logger.info(
                "Dispatching trigger",
                extra={"trigger_id": trigger.id, "agent": trigger.agent_name, "scheduled_for": occurrence.scheduled_for},
            )
            key = (trigger.id, occurrence.scheduled_for)
            if ticket is not None:
                result = await manager.run_registered(ticket, trigger_occurrence=key)
            else:
                result = await manager.execute_agent(trigger.agent_name, occurrence.instructions,
                                                     request_id=occurrence.run_id, trigger_occurrence=key)
            if result.success:
                self._handle_success(trigger, occurrence.fired_at)
                status = "done"
            else:
                self._handle_failure(trigger, occurrence.fired_at, result.error or result.response)
        except asyncio.CancelledError:
            # Shutdown or crash mid-run: leave the claim 'claimed' so the next start
            # reports this occurrence as interrupted instead of quietly calling it failed.
            cancelled = True
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._handle_failure(trigger, _utc_now(), str(exc))
            logger.exception("Trigger execution failed unexpectedly", extra={"trigger_id": trigger.id})
        finally:
            self._in_flight.discard(trigger.id)
            if flags.enabled("SEND_LEDGER") and not cancelled:
                from .safety.trigger_claims import get_trigger_claims

                get_trigger_claims().finish(trigger.id, occurrence.scheduled_for, status)

    def _advance_without_running(self, trigger_id: int, scheduled_for: str) -> Optional[TriggerRecord]:
        record = self._service.get_trigger(trigger_id)
        if record is None:
            return None
        if record.recurrence_rule:
            return self._service.schedule_next_occurrence(record, fired_at=parse_iso(scheduled_for))
        return self._service.clear_next_fire(record.id, agent_name=record.agent_name)

    def _recover_abandoned_occurrences(self) -> None:
        """Decision 11 / U10: an occurrence that was running when the process died is
        advanced and REPORTED to the user, never silently re-run or dropped."""
        from .safety.ledger import describe_side_effect, get_ledger
        from .safety.trigger_claims import get_trigger_claims

        abandoned = get_trigger_claims().sweep_abandoned()
        if not abandoned:
            return
        lines = []
        for claim in abandoned:
            record = self._service.get_trigger(claim.trigger_id)
            self._advance_without_running(claim.trigger_id, claim.scheduled_for)
            agent = record.agent_name if record else "an agent"
            telemetry.emit("trigger_abandoned", trigger_id=claim.trigger_id, scheduled_for=claim.scheduled_for)
            line = (f"A scheduled task for {agent} (trigger {claim.trigger_id}, due {claim.scheduled_for}) "
                    "was interrupted by a restart and was not re-run.")
            if claim.run_id:
                summary = get_ledger().run_summary(claim.run_id)
                if summary.sent:
                    line += " Before the restart it had already done: " + "; ".join(
                        describe_side_effect(r, with_time=True) for r in summary.sent) + "."
                if summary.in_doubt:
                    line += " Outcome unknown for: " + "; ".join(describe_side_effect(r) for r in summary.in_doubt) + \
                            ". Do not resend without the user's confirmation."
            lines.append(line)
        message = "\n".join(lines)

        async def report() -> None:
            from ..agents.interaction_agent.runtime import InteractionAgentRuntime

            with turn_scope("trigger"):
                await InteractionAgentRuntime().handle_agent_message(message, origin="trigger")

        tasks.spawn(report(), name="abandoned-trigger-report")

    def _sweep_stale_sends(self) -> None:
        try:
            from .safety.ledger import get_ledger

            get_ledger().sweep_stale_pending()
        except Exception:  # pragma: no cover - a sweep failure must not stop the scheduler
            logger.exception("Stale-send sweep failed")

    def _handle_success(self, trigger: TriggerRecord, fired_at: datetime) -> None:
        logger.info(
            "Trigger completed",
            extra={"trigger_id": trigger.id, "agent": trigger.agent_name},
        )
        self._service.schedule_next_occurrence(trigger, fired_at=fired_at)

    def _handle_failure(self, trigger: TriggerRecord, fired_at: datetime, error: str) -> None:
        logger.warning(
            "Trigger execution failed",
            extra={
                "trigger_id": trigger.id,
                "agent": trigger.agent_name,
                "error": error,
            },
        )
        self._service.record_failure(trigger, error)
        if trigger.recurrence_rule:
            self._service.schedule_next_occurrence(trigger, fired_at=fired_at)
        else:
            self._service.clear_next_fire(trigger.id, agent_name=trigger.agent_name)

    def _format_instructions(self, trigger: TriggerRecord, fired_at: datetime) -> str:
        scheduled_for = trigger.next_trigger or _isoformat(fired_at)
        metadata_lines = [f"Trigger ID: {trigger.id}"]
        if trigger.recurrence_rule:
            metadata_lines.append(f"Recurrence: {trigger.recurrence_rule}")
        if trigger.timezone:
            metadata_lines.append(f"Timezone: {trigger.timezone}")
        if trigger.start_time:
            metadata_lines.append(f"Start Time (UTC): {trigger.start_time}")

        metadata = "\n".join(f"- {line}" for line in metadata_lines)
        return (
            f"Trigger fired at {_isoformat(fired_at)} (UTC).\n"
            f"Scheduled occurrence time: {scheduled_for}.\n\n"
            f"Metadata:\n{metadata}\n\n"
            f"Payload:\n{trigger.payload}"
        )


_scheduler_instance: Optional[TriggerScheduler] = None


def get_trigger_scheduler() -> TriggerScheduler:
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = TriggerScheduler()
    return _scheduler_instance


__all__ = ["TriggerScheduler", "get_trigger_scheduler"]
