"""Coordinate execution agents and batch their results for the interaction agent.

Flag-scoped behaviour (all off = the original single global batch):
- AGENT_LOCK (1.6): one run per agent at a time; the request is journaled inside the lock.
- SEND_LEDGER (1.8): honest [PARTIAL]/[UNKNOWN] reports when a run that sent email stops.
- AGENT_CARD (2.2): the agent card is built before, and updated after, each run (under the lock).
- TURN_BATCHES (2.6): one batch per originating turn instead of one global batch.
- POLL_BATCH (2.7): callers can register several runs before any starts (one flush per poll).
- CONCURRENCY_CAP (2.9): global run gate (after the agent lock).
Timeouts start only after the lock and the gate are held (I12).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .runtime import ExecutionAgentRuntime, ExecutionResult
from ... import flags
from ...logging_config import logger
from ...overload_config import EA_RUN_TIMEOUT_S, POLL_BATCH_MAX_WAIT_S
from ...services import telemetry
from ...services.context import TurnContext, current_turn, run_scope, turn_scope
from ...utils import tasks


@dataclass
class PendingExecution:
    """Track a pending execution request."""

    request_id: str
    agent_name: str
    instructions: str
    batch_id: str
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class _BatchState:
    """Collect results for a single interaction-agent turn."""

    batch_id: str
    created_at: datetime = field(default_factory=datetime.now)
    pending: int = 0
    results: List[ExecutionResult] = field(default_factory=list)
    # The turn that opened the batch; the follow-up turn is its child.
    turn: Optional[TurnContext] = None
    max_wait_handle: Optional[asyncio.TimerHandle] = None


@dataclass(frozen=True)
class Ticket:
    """A registered (counted-in-a-batch) execution that has not necessarily started."""

    request_id: str
    agent_name: str
    instructions: str
    batch_id: str
    batch_key: Optional[str]  # turn id with TURN_BATCHES, None for the global batch
    registered_at: float


class ExecutionBatchManager:
    """Run execution agents and deliver their combined outcome."""

    # Initialize batch manager with timeout and coordination state for execution agents
    def __init__(self, timeout_seconds: Optional[float] = None) -> None:
        # EA_RUN_TIMEOUT_S defaults to the original 90 s.
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else EA_RUN_TIMEOUT_S.get()
        self._pending: Dict[str, PendingExecution] = {}
        self._batch_lock = asyncio.Lock()
        self._batch_state: Optional[_BatchState] = None
        self._batches: Dict[str, _BatchState] = {}  # TURN_BATCHES: keyed by originating turn id

    # Run execution agent with timeout handling and batch coordination for interaction agent
    async def execute_agent(
        self,
        agent_name: str,
        instructions: str,
        request_id: Optional[str] = None,
        *,
        record_request: bool = False,
        trigger_occurrence: Optional[Tuple[int, str]] = None,
    ) -> ExecutionResult:
        """Execute an agent asynchronously and buffer the result for batch dispatch."""

        ticket = await self.register(agent_name, instructions, request_id)
        return await self.run_registered(ticket, record_request=record_request, trigger_occurrence=trigger_occurrence)

    async def register(self, agent_name: str, instructions: str, request_id: Optional[str] = None) -> Ticket:
        """Count an execution in the current batch without starting it."""
        request_id = request_id or str(uuid.uuid4())
        batch_id, batch_key = await self._register_pending_execution(agent_name, instructions, request_id)
        return Ticket(request_id, agent_name, instructions, batch_id, batch_key, time.monotonic())

    async def run_registered(
        self,
        ticket: Ticket,
        *,
        record_request: bool = False,
        trigger_occurrence: Optional[Tuple[int, str]] = None,
    ) -> ExecutionResult:
        agent_name = ticket.agent_name
        try:
            result = await self._admit_and_run(ticket, record_request, trigger_occurrence)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(f"[{agent_name}] Execution failed unexpectedly")
            result = ExecutionResult(
                agent_name=agent_name,
                success=False,
                response=f"Execution failed: {exc}",
                error=str(exc),
            )
        finally:
            self._pending.pop(ticket.request_id, None)

        await self._complete_execution(ticket, result, agent_name)
        return result

    async def _admit_and_run(
        self, ticket: Ticket, record_request: bool, trigger_occurrence: Optional[Tuple[int, str]]
    ) -> ExecutionResult:
        """Agent lock -> concurrency gate -> (deadline starts) run -> card update, all under the lock."""
        lock = nullcontext()
        if flags.enabled("AGENT_LOCK"):
            from .admission import agent_lock

            lock = agent_lock(ticket.agent_name)
        gate = nullcontext()
        if flags.enabled("CONCURRENCY_CAP"):
            from .admission import get_gate

            gate = get_gate().slot(current_turn().root_origin)

        async with lock:
            async with gate:
                if record_request:
                    # 1.6: journal the request only once the run owns the agent, so queued
                    # requests never appear in a running agent's history and journals alternate.
                    from ...services.execution import get_execution_agent_logs

                    get_execution_agent_logs().record_request(ticket.agent_name, ticket.instructions)
                return await self._run_with_timeout(ticket, trigger_occurrence)

    async def _run_with_timeout(self, ticket: Ticket, trigger_occurrence: Optional[Tuple[int, str]]) -> ExecutionResult:
        agent_name = ticket.agent_name
        card_text = self._card_before_run(agent_name)
        runtime: Optional[ExecutionAgentRuntime] = None
        outcome = "failed"
        started = time.monotonic()
        with run_scope(
            agent_name,
            run_id=ticket.request_id,
            deadline_monotonic=started + self.timeout_seconds,
            trigger_occurrence=trigger_occurrence,
        ):
            telemetry.emit(
                "run_start",
                queued_ms=round((started - ticket.registered_at) * 1000, 1),
                timeout_s=self.timeout_seconds,
            )
            try:
                logger.info(f"[{agent_name}] Execution started")
                runtime = ExecutionAgentRuntime(agent_name=agent_name, card_text=card_text)
                result = await asyncio.wait_for(
                    runtime.execute(ticket.instructions),
                    timeout=self.timeout_seconds,
                )
                outcome = "success" if result.success else "failed"
                status = "SUCCESS" if result.success else "FAILED"
                logger.info(f"[{agent_name}] Execution finished: {status}")
            except asyncio.TimeoutError:
                outcome = "timeout"
                logger.error(f"[{agent_name}] Execution timed out after {self.timeout_seconds}s")
                result = ExecutionResult(
                    agent_name=agent_name,
                    success=False,
                    response=f"Execution timed out after {self.timeout_seconds} seconds",
                    error="Timeout",
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(f"[{agent_name}] Execution failed unexpectedly")
                result = ExecutionResult(
                    agent_name=agent_name,
                    success=False,
                    response=f"Execution failed: {exc}",
                    error=str(exc),
                )

            if flags.enabled("SEND_LEDGER"):
                result = self._honest_result(result, ticket.request_id, timed_out=outcome == "timeout")
                if result.outcome:
                    outcome = result.outcome

            telemetry.emit(
                "run_end",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                outcome=outcome,
                timeout_s=self.timeout_seconds,
            )
            self._card_after_run(agent_name, ticket.instructions, runtime, result)
        return result

    # ------------------------------------------------------------ 1.8 honesty
    def _honest_result(self, result: ExecutionResult, run_id: str, *, timed_out: bool) -> ExecutionResult:
        """Never report plain [FAILED] for a run that sent or started an email (I4), and never
        plain [SUCCESS] for one that left a send with an unknown outcome."""
        from ...services.execution import get_execution_agent_logs
        from ...services.safety.ledger import describe_side_effect, get_ledger

        ledger = get_ledger()
        summary = ledger.run_summary(run_id)
        if result.success:
            if not summary.in_doubt:
                return result
            ledger.mark_reported_unknown([row.id for row in summary.in_doubt])
            note = ("Outcome unknown for: " + "; ".join(describe_side_effect(r) for r in summary.in_doubt)
                    + ". Do not retry; the user must confirm.")
            return ExecutionResult(agent_name=result.agent_name, success=False, response=f"{result.response}\n{note}",
                                   error=result.error, tools_executed=result.tools_executed, outcome="unknown")
        stopped = "Timed out" if timed_out else "Stopped"
        if summary.in_doubt:
            first = summary.in_doubt[0]
            text = (f"{stopped} while {describe_side_effect(first)} was in progress; outcome unknown. "
                    "Do not retry; the user must confirm.")
            if summary.sent:
                text += " Completed before stopping: " + "; ".join(describe_side_effect(r, with_time=True) for r in summary.sent) + "."
            ledger.mark_reported_unknown([row.id for row in summary.in_doubt])
            outcome = "unknown"
        elif summary.sent:
            text = ("Completed before stopping: " + "; ".join(describe_side_effect(r, with_time=True) for r in summary.sent)
                    + ". Remaining work did not finish.")
            outcome = "partial"
        else:
            return result
        if timed_out:  # the runtime was cancelled before it could journal anything
            get_execution_agent_logs().record_agent_response(result.agent_name, f"[{outcome.upper()}] {text}")
        return ExecutionResult(agent_name=result.agent_name, success=False, response=text, error=result.error,
                               tools_executed=result.tools_executed, outcome=outcome)

    # ------------------------------------------------------------ 2.2 card hooks
    def _card_before_run(self, agent_name: str) -> Optional[str]:
        if not flags.enabled("AGENT_CARD"):
            return None
        from ...services.agents import card

        return card.render_for_prompt(agent_name)

    def _card_after_run(self, agent_name: str, instructions: str, runtime: Optional[ExecutionAgentRuntime],
                        result: ExecutionResult) -> None:
        if not flags.enabled("AGENT_CARD"):
            return
        from ...services.agents import card

        card.update_after_run(agent_name, instructions, runtime, result)

    # ------------------------------------------------------------ batching
    # Add execution request to current batch or create new batch if none exists
    async def _register_pending_execution(
        self,
        agent_name: str,
        instructions: str,
        request_id: str,
    ) -> Tuple[str, Optional[str]]:
        """Attach a new execution to the active batch, opening one when required."""

        async with self._batch_lock:
            if flags.enabled("TURN_BATCHES"):
                # 2.6 (M6): results are batched per originating turn, so unrelated work never waits.
                turn = current_turn()
                key: Optional[str] = turn.turn_id
                state = self._batches.get(key)
                if state is None:
                    state = _BatchState(batch_id=str(uuid.uuid4()), turn=turn)
                    self._batches[key] = state
                    self._arm_max_wait(key, state)
                batch_id = state.batch_id
            else:
                key = None
                if self._batch_state is None:
                    batch_id = str(uuid.uuid4())
                    self._batch_state = _BatchState(batch_id=batch_id, turn=current_turn())
                else:
                    batch_id = self._batch_state.batch_id
                state = self._batch_state

            state.pending += 1
            self._pending[request_id] = PendingExecution(
                request_id=request_id,
                agent_name=agent_name,
                instructions=instructions,
                batch_id=batch_id,
            )

            return batch_id, key

    def _state_for(self, ticket_or_key) -> Optional[_BatchState]:
        key = ticket_or_key.batch_key if isinstance(ticket_or_key, Ticket) else ticket_or_key
        if key is None:
            return self._batch_state
        return self._batches.get(key)

    # Store execution result and send combined batch to interaction agent when complete
    async def _complete_execution(
        self,
        ticket: Ticket,
        result: ExecutionResult,
        agent_name: str,
    ) -> None:
        """Record the execution result and dispatch when the batch drains."""

        dispatch_payload: Optional[str] = None
        batch_turn: Optional[TurnContext] = None

        async with self._batch_lock:
            state = self._state_for(ticket)
            if state is None or state.batch_id != ticket.batch_id:
                logger.warning(f"[{agent_name}] Dropping result for unknown batch")
                return

            state.results.append(result)
            state.pending -= 1

            if state.pending == 0:
                if state.max_wait_handle is not None:
                    state.max_wait_handle.cancel()
                agent_names = [entry.agent_name for entry in state.results]
                if state.results:
                    dispatch_payload = self._format_batch_payload(state.results)
                    logger.info(f"Execution batch completed: {', '.join(agent_names)}")
                    telemetry.emit("batch_flush", items=len(state.results), agents=agent_names, batch_id=state.batch_id)
                batch_turn = state.turn
                if ticket.batch_key is None:
                    self._batch_state = None
                else:
                    self._batches.pop(ticket.batch_key, None)

        if dispatch_payload:
            await self._dispatch_to_interaction_agent(dispatch_payload, batch_turn)

    def _arm_max_wait(self, key: str, state: _BatchState) -> None:
        """2.7 option: flush a poll's finished results after POLL_BATCH_MAX_WAIT_S (0 = disabled)."""
        max_wait = float(POLL_BATCH_MAX_WAIT_S.get())
        if max_wait <= 0 or not flags.enabled("POLL_BATCH") or state.turn is None or state.turn.origin != "trigger":
            return
        loop = asyncio.get_running_loop()
        state.max_wait_handle = loop.call_later(
            max_wait, lambda: tasks.spawn(self._flush_partial(key, state.batch_id), name="batch-max-wait")
        )

    async def _flush_partial(self, key: str, batch_id: str) -> None:
        async with self._batch_lock:
            state = self._batches.get(key)
            if state is None or state.batch_id != batch_id or not state.results or state.pending == 0:
                return
            payload = self._format_batch_payload(state.results)
            telemetry.emit("batch_flush", items=len(state.results), agents=[r.agent_name for r in state.results],
                           batch_id=batch_id, partial=True)
            state.results = []
            turn = state.turn
        await self._dispatch_to_interaction_agent(payload, turn)

    # Return list of currently pending execution requests for monitoring purposes
    def get_pending_executions(self) -> List[Dict[str, str]]:
        """Expose pending executions for observability."""

        return [
            {
                "request_id": pending.request_id,
                "agent_name": pending.agent_name,
                "batch_id": pending.batch_id,
                "created_at": pending.created_at.isoformat(),
                "elapsed_seconds": (datetime.now() - pending.created_at).total_seconds(),
            }
            for pending in self._pending.values()
        ]

    def open_batches(self) -> int:
        """Batches still waiting for results (tests assert this drains to 0)."""
        return len(self._batches) + (1 if self._batch_state is not None else 0)

    # Clean up all pending executions and batch state on shutdown
    async def shutdown(self) -> None:
        """Clear pending bookkeeping (no background work remains)."""

        self._pending.clear()
        async with self._batch_lock:
            self._batch_state = None
            self._batches.clear()

    def reset_for_tests(self) -> None:
        """Drop all in-memory state and rebuild loop-bound primitives (tests only)."""
        self._pending.clear()
        self._batch_lock = asyncio.Lock()
        self._batch_state = None
        self._batches.clear()
        self.timeout_seconds = EA_RUN_TIMEOUT_S.get()

    # Format multiple execution results into single message for interaction agent
    def _format_batch_payload(self, results: List[ExecutionResult]) -> str:
        """Render execution results into the interaction-agent format."""

        entries: List[str] = []
        for result in results:
            status = "SUCCESS" if result.success else "FAILED"
            if result.outcome:  # SEND_LEDGER honest outcomes: PARTIAL / UNKNOWN
                status = result.outcome.upper()
            response_text = (result.response or "(no response provided)").strip()
            entries.append(f"[{status}] {result.agent_name}: {response_text}")
        return "\n".join(entries)

    # Forward combined execution results to interaction agent for user response generation
    async def _dispatch_to_interaction_agent(self, payload: str, parent: Optional[TurnContext] = None) -> None:
        """Send the aggregated execution summary to the interaction agent."""

        from ..interaction_agent.runtime import InteractionAgentRuntime

        runtime = InteractionAgentRuntime()

        async def _follow_up_turn() -> None:
            # create_task copies the finishing run's context; open a fresh child
            # turn of the batch's turn first so correlation is right.
            with turn_scope("agent", parent=parent):
                await runtime.handle_agent_message(payload)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_follow_up_turn())
            return

        if flags.enabled("TURN_BATCHES"):
            tasks.spawn(_follow_up_turn(), name="ia-follow-up")  # tracked (I11)
        else:
            loop.create_task(_follow_up_turn())


_batch_manager: Optional[ExecutionBatchManager] = None


def get_batch_manager() -> ExecutionBatchManager:
    """The process-wide batch manager used for IA dispatches (and, with POLL_BATCH, triggers)."""
    global _batch_manager
    if _batch_manager is None:
        _batch_manager = ExecutionBatchManager()
    return _batch_manager
