"""Structured telemetry (single owner).

``emit(event, **fields)`` attaches correlation fields (turn, parent, origin,
root origin, depth, poll, run, agent) plus wall and monotonic timestamps and
writes the event to two sinks:

- an in-memory ring (tests use :func:`capture`);
- JSONL at ``data_dir()/telemetry/events.jsonl`` (rotated at 20 MB, one backup),
  disabled with ``OPENPOKE_TELEMETRY=0``.

Rules (S5, I10):
- sizes and hashes only, never message or email content;
- ``emit`` never raises; failures are counted in :data:`dropped` and a
  ``telemetry_dropped`` event goes to the in-memory ring.

Thread safe: Gmail executor threads emit too.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, List, Optional

from .. import flags
from ..overload_config import TELEMETRY_MAX_BYTES, TELEMETRY_RING_SIZE
from ..paths import data_dir
from .context import context_fields, current_run, current_turn

_lock = threading.RLock()
_ring: Deque[Dict[str, Any]] = deque(maxlen=int(TELEMETRY_RING_SIZE.get()))
_captures: List[List[Dict[str, Any]]] = []
dropped = 0  # events that failed to reach the file sink (or failed entirely)


def sha(text: Any, length: int = 16) -> str:
    """Stable short content hash for correlating without storing content."""
    data = text if isinstance(text, (bytes, bytearray)) else str(text).encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()[:length]


def events_path() -> Path:
    return data_dir() / "telemetry" / "events.jsonl"


def _rotate_if_needed(path: Path, incoming: int) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size + incoming <= TELEMETRY_MAX_BYTES.get():
        return
    backup = path.with_suffix(path.suffix + ".1")
    if backup.exists():
        backup.unlink()
    path.replace(backup)


def _write_file(record: Dict[str, Any]) -> None:
    line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path, len(line))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _record_drop(event: str, exc: BaseException) -> None:
    global dropped
    dropped += 1
    try:
        _ring.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "mono": time.monotonic(),
                "event": "telemetry_dropped",
                "dropped_event": event,
                "error_class": type(exc).__name__,
                "dropped_total": dropped,
            }
        )
    except Exception:  # pragma: no cover - last resort
        pass


def emit(event: str, **fields: Any) -> None:
    """Record one telemetry event. Never raises (I10)."""
    try:
        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mono": time.monotonic(),
            "event": event,
        }
        record.update(context_fields())
        record.update(fields)
    except Exception as exc:  # pragma: no cover - defensive
        with _lock:
            _record_drop(event, exc)
        return

    with _lock:
        try:
            _ring.append(record)
            for bucket in _captures:
                bucket.append(record)
        except Exception as exc:  # pragma: no cover - defensive
            _record_drop(event, exc)
            return
        if not flags.telemetry_file_enabled():
            return
        try:
            _write_file(record)
        except Exception as exc:
            _record_drop(event, exc)


@contextmanager
def capture() -> Iterator[List[Dict[str, Any]]]:
    """Collect every event emitted while the block runs (tests)."""
    bucket: List[Dict[str, Any]] = []
    with _lock:
        _captures.append(bucket)
    try:
        yield bucket
    finally:
        with _lock:
            if bucket in _captures:
                _captures.remove(bucket)


def recent(event: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        items = list(_ring)
    return [item for item in items if event is None or item.get("event") == event]


def reset() -> None:
    """Clear in-memory state (tests)."""
    global dropped
    with _lock:
        _ring.clear()
        _captures.clear()
        dropped = 0


# ---------------------------------------------------------------------------
# Eval-only prompt capture (OPENPOKE_EVAL_CAPTURE_PROMPTS=1 requires fake Gmail)
# ---------------------------------------------------------------------------

_capture_counter_lock = threading.Lock()
_capture_counter = 0


def prompt_capture_enabled() -> bool:
    return flags.eval_capture_prompts_enabled() and flags.fake_gmail_enabled()


def capture_prompt(role: str, round_index: int, payload: Dict[str, Any]) -> None:
    """Write a full prompt to disk for eval metrics (context_hit). Never raises.

    Refused (silently skipped) unless fake Gmail is on, so real email content is
    never written; flags.validate() additionally fails fast at startup.
    """
    if not prompt_capture_enabled():
        return
    global _capture_counter
    try:
        run = current_run()
        turn = current_turn()
        key = run.run_id if run else turn.turn_id
        with _capture_counter_lock:
            _capture_counter += 1
            seq = _capture_counter
        directory = data_dir() / "telemetry" / "prompts"
        directory.mkdir(parents=True, exist_ok=True)
        record = {"role": role, "round": round_index, "seq": seq, **context_fields(), **payload}
        target = directory / f"{key}.{round_index}.{seq}.json"
        target.write_text(json.dumps(record, default=str, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        with _lock:
            _record_drop("prompt_capture", exc)


def read_events(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load a JSONL events file (eval metrics)."""
    target = path or events_path()
    events: List[Dict[str, Any]] = []
    if not target.exists():
        return events
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


__all__ = [
    "capture",
    "capture_prompt",
    "dropped",
    "emit",
    "events_path",
    "prompt_capture_enabled",
    "read_events",
    "recent",
    "reset",
    "sha",
]
