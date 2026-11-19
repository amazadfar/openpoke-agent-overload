"""Every tunable constant used by the agent-overload fixes (single owner).

Rules:
- No magic numbers elsewhere: code reads values through ``<CONSTANT>.get()``.
- Each constant is env-overridable as ``OPENPOKE_<NAME>`` and read at call time.
- Each constant carries its rationale; ``docs/overload/02_DESIGN.md`` Appendix B
  lists the same table (``python -m server.overload_config`` prints it).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

from .logging_config import logger

Number = Union[int, float]
ENV_PREFIX = "OPENPOKE_"


@dataclass(frozen=True)
class Constant:
    """A named, documented, env-overridable tunable."""

    name: str
    default: Number
    kind: type
    rationale: str
    minimum: Optional[Number] = None

    @property
    def env_var(self) -> str:
        return ENV_PREFIX + self.name

    def _parse(self, raw: str) -> Number:
        number = float(raw)
        if self.kind is int:
            if number != int(number):
                raise ValueError("expected an integer")
            value: Number = int(number)
        else:
            value = number
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"must be >= {self.minimum}")
        return value

    def get(self) -> Number:
        """Current value: env override if valid, otherwise the documented default."""
        raw = os.environ.get(self.env_var)
        if raw is None or raw.strip() == "":
            return self.default
        try:
            return self._parse(raw.strip())
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring invalid %s=%r (%s); using default %s", self.env_var, raw, exc, self.default)
            return self.default

    def validate(self) -> Optional[str]:
        raw = os.environ.get(self.env_var)
        if raw is None or raw.strip() == "":
            return None
        try:
            self._parse(raw.strip())
        except (TypeError, ValueError) as exc:
            return f"{self.env_var}={raw!r}: {exc}"
        return None


_REGISTRY: Dict[str, Constant] = {}


def _const(name: str, default: Number, rationale: str, *, minimum: Optional[Number] = None) -> Constant:
    constant = Constant(name=name, default=default, kind=type(default), rationale=rationale, minimum=minimum)
    _REGISTRY[name] = constant
    return constant


# --- 1.2 EA history window (M2) ------------------------------------------------
EA_HISTORY_REQUESTS = _const(
    "EA_HISTORY_REQUESTS", 5,
    "Requests of journal history kept in an execution agent prompt (U3). Measured: K=5 keeps the "
    "prompt flat at ~5.2k tokens (est.) where the full journal reaches ~155k tokens at 180 runs.",
    minimum=1,
)
EA_HISTORY_MAX_CHARS = _const(
    "EA_HISTORY_MAX_CHARS", 40000,
    "Safety cap (~10k tokens est.) on rendered history after the request window, for single huge entries.",
    minimum=1000,
)

# --- 1.4 LLM retries (M4) ------------------------------------------------------
LLM_RETRY_MAX_ATTEMPTS = _const(
    "LLM_RETRY_MAX_ATTEMPTS", 3,
    "Total attempts per LLM call. A small cap avoids amplifying a rate-limit storm.",
    minimum=1,
)
LLM_RETRY_BASE_S = _const(
    "LLM_RETRY_BASE_S", 1.0,
    "Base of the exponential backoff (full jitter): delay = uniform(0, min(max, base * 2^n)).",
    minimum=0.0,
)
LLM_RETRY_MAX_DELAY_S = _const(
    "LLM_RETRY_MAX_DELAY_S", 20.0,
    "Cap on one backoff delay and on honoured Retry-After; well under the 90 s run deadline.",
    minimum=0.0,
)
LLM_MIN_CALL_BUDGET_S = _const(
    "LLM_MIN_CALL_BUDGET_S", 10.0,
    "Do not start a retry that cannot get this much time before the run deadline.",
    minimum=0.0,
)
LLM_REQUEST_TIMEOUT_S = _const(
    "LLM_REQUEST_TIMEOUT_S", 60.0,
    "Existing per-request HTTP timeout (openrouter_client/client.py); unchanged.",
    minimum=1.0,
)
LLM_MIN_ATTEMPT_TIMEOUT_S = _const(
    "LLM_MIN_ATTEMPT_TIMEOUT_S", 5.0,
    "Floor for the per-attempt timeout when it is derived from a run deadline (min(60, remaining-1)).",
    minimum=1.0,
)

# --- Execution runs --------------------------------------------------------------
EA_RUN_TIMEOUT_S = _const(
    "EA_RUN_TIMEOUT_S", 90,
    "Existing per-run timeout (batch_manager.py default); unchanged.",
    minimum=1.0,
)

# --- 1.5 / 1.9 Gmail executor (M5) -------------------------------------------------
GMAIL_WORKERS = _const(
    "GMAIL_WORKERS", 1,
    "Composio client thread safety is unverified (U5): one worker keeps calls serialized, as they "
    "effectively are today, while freeing the event loop.",
    minimum=1,
)
MIN_SEND_BUDGET_S = _const(
    "MIN_SEND_BUDGET_S", 15.0,
    "Do not start an external send that the run deadline may cut off mid-flight.",
    minimum=0.0,
)
SHUTDOWN_GRACE_S = _const(
    "SHUTDOWN_GRACE_S", 10.0,
    "How long shutdown waits for in-flight sends before leaving them pending (U9).",
    minimum=0.0,
)

# --- 1.7 D1 dispatch dedupe ----------------------------------------------------------
DISPATCH_DEDUPE_WINDOW_S = _const(
    "DISPATCH_DEDUPE_WINDOW_S", 120.0,
    "Exact repeats are suppressed while in flight plus this long after completion (U6): covers "
    "concurrent-turn duplicates, short enough not to block a deliberate 'check again'.",
    minimum=0.0,
)

# --- 1.8 D2 ledger ---------------------------------------------------------------------
NEAR_DUP_WINDOW_S = _const(
    "NEAR_DUP_WINDOW_S", 86400.0,
    "Near-duplicate sends (same thread/recipients, different body) need confirmation within 24 h (U7).",
    minimum=0.0,
)
PENDING_STALE_AFTER_S = _const(
    "PENDING_STALE_AFTER_S", 600.0,
    "A send still 'pending' after this long has lost its outcome (longer than any 90 s run plus slack); "
    "the runtime sweep marks it 'unknown'. Process start sweeps every pending row (nothing can be in flight).",
    minimum=0.0,
)
SQLITE_BUSY_RETRIES = _const(
    "SQLITE_BUSY_RETRIES", 3,
    "Attempts for a safety-database operation that hits 'database is locked' before refusing to send.",
    minimum=1,
)
SQLITE_BUSY_SLEEP_S = _const(
    "SQLITE_BUSY_SLEEP_S", 0.05,
    "Short sleep between safety-database retries.",
    minimum=0.0,
)

# --- 1.10 send gate -------------------------------------------------------------------
APPROVAL_TTL_S = _const(
    "APPROVAL_TTL_S", 900.0,
    "An approval code is valid for 15 minutes.",
    minimum=1.0,
)
APPROVAL_CODE_LENGTH = _const(
    "APPROVAL_CODE_LENGTH", 6,
    "Approval code length over a 31-symbol unambiguous alphabet (31^6 ~ 8.9e8): unguessable within its TTL, "
    "short enough to type.",
    minimum=4,
)
APPROVAL_SUMMARY_BODY_CHARS = _const(
    "APPROVAL_SUMMARY_BODY_CHARS", 200,
    "Body chars shown to the user in an approval summary.",
    minimum=20,
)

# --- 2.2 agent card -----------------------------------------------------------------
CARD_MAX_COUNTERPARTIES = _const(
    "CARD_MAX_COUNTERPARTIES", 10,
    "Most recent counterparty emails shown on a card (bounds card size).",
    minimum=1,
)
CARD_MAX_THREADS = _const(
    "CARD_MAX_THREADS", 10,
    "Most recent thread IDs shown on a card (bounds card size).",
    minimum=1,
)
CARD_PURPOSE_CHARS = _const(
    "CARD_PURPOSE_CHARS", 200,
    "Chars kept from the first and the latest request for the card purpose.",
    minimum=20,
)
CARD_OUTCOME_CHARS = _const(
    "CARD_OUTCOME_CHARS", 300,
    "Chars kept from the last agent response on the card.",
    minimum=20,
)
IDS_PER_KIND_CAP = _const(
    "IDS_PER_KIND_CAP", 50,
    "Identifiers kept per kind per tool call; bounds journal growth on huge search results.",
    minimum=1,
)

# --- 2.3 resolver -------------------------------------------------------------------
RESOLVER_MAX_CANDIDATES = _const(
    "RESOLVER_MAX_CANDIDATES", 3,
    "Candidates returned to the IA on an ambiguous name (a short list the model can read).",
    minimum=1,
)

# --- 2.4 Top-K roster -----------------------------------------------------------------
TOPK_K = _const(
    "TOPK_K", 8,
    "Agents rendered in the IA roster (~8 x 60 tokens est.) instead of every agent ever created.",
    minimum=1,
)
TOPK_HOT = _const(
    "TOPK_HOT", 3,
    "Of the K, this many are the most recently active agents regardless of relevance.",
    minimum=0,
)
TOPK_PURPOSE_CHARS = _const(
    "TOPK_PURPOSE_CHARS", 120,
    "Purpose chars shown per roster entry.",
    minimum=20,
)
BM25_K1 = _const("BM25_K1", 1.2, "Textbook BM25 default; not tuned.", minimum=0.0)
BM25_B = _const("BM25_B", 0.75, "Textbook BM25 default; not tuned.", minimum=0.0)

# --- 2.5 dormancy ---------------------------------------------------------------------
DORMANCY_DAYS = _const(
    "DORMANCY_DAYS", 14.0,
    "An agent idle this long with no active trigger is hidden from the roster but still resolvable (U8).",
    minimum=0.0,
)

# --- 2.7 poll batching ----------------------------------------------------------------
POLL_BATCH_MAX_WAIT_S = _const(
    "POLL_BATCH_MAX_WAIT_S", 0.0,
    "0 = disabled. If set, a poll's batch flushes what has finished after this long. Enable only with evidence.",
    minimum=0.0,
)

# --- 2.8 watcher ------------------------------------------------------------------------
WATCHER_BUFFER_MAX = _const(
    "WATCHER_BUFFER_MAX", 50,
    "Buffered important-email summaries; equals the watcher's max_results per fetch (U9).",
    minimum=1,
)
WATCHER_CLASSIFY_MAX_ATTEMPTS = _const(
    "WATCHER_CLASSIFY_MAX_ATTEMPTS", 3,
    "Classification attempts per email before it is dropped with a warning event (U9).",
    minimum=1,
)
WATCHER_SHUTDOWN_GRACE_S = _const(
    "WATCHER_SHUTDOWN_GRACE_S", 10.0,
    "stop() waits this long for an in-flight watcher turn before cancelling it (U9).",
    minimum=0.0,
)
WATCHER_TURN_REQUEUE_MAX = _const(
    "WATCHER_TURN_REQUEUE_MAX", 1,
    "A failed watcher turn's batch is re-enqueued this many times, then dropped with telemetry.",
    minimum=0,
)

# --- 2.9 concurrency gate -------------------------------------------------------------------
CONCURRENCY_TOTAL = _const(
    "CONCURRENCY_TOTAL", 4,
    "Maximum concurrent execution-agent runs (U4; provisional, adjust from observed 429s).",
    minimum=1,
)
CONCURRENCY_USER_RESERVED = _const(
    "CONCURRENCY_USER_RESERVED", 1,
    "Slots only user-rooted runs may take, so a trigger/watcher storm cannot starve the user (U4).",
    minimum=0,
)

# --- Telemetry --------------------------------------------------------------------------------
TELEMETRY_MAX_BYTES = _const(
    "TELEMETRY_MAX_BYTES", 20 * 1024 * 1024,
    "Rotate events.jsonl at 20 MB, keeping one backup, so telemetry cannot fill the disk.",
    minimum=1024,
)
TELEMETRY_RING_SIZE = _const(
    "TELEMETRY_RING_SIZE", 10000,
    "In-memory event ring (tests and debugging).",
    minimum=10,
)


def all_constants() -> List[Constant]:
    return list(_REGISTRY.values())


def validate() -> None:
    """Raise ValueError on invalid overrides or inconsistent combinations."""
    problems = [msg for msg in (c.validate() for c in _REGISTRY.values()) if msg]
    if CONCURRENCY_USER_RESERVED.get() >= CONCURRENCY_TOTAL.get():
        problems.append("CONCURRENCY_USER_RESERVED must be smaller than CONCURRENCY_TOTAL")
    if TOPK_HOT.get() > TOPK_K.get():
        problems.append("TOPK_HOT must not exceed TOPK_K")
    if problems:
        raise ValueError("Invalid OpenPoke constants: " + "; ".join(problems))


def render_markdown_table(value_of: Callable[[Constant], Number] = lambda c: c.get()) -> str:
    lines = ["| Constant (env `OPENPOKE_<NAME>`) | Value | Rationale |", "|---|---|---|"]
    for constant in _REGISTRY.values():
        lines.append(f"| `{constant.name}` | {value_of(constant)} | {constant.rationale} |")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - documentation helper
    print(render_markdown_table())
