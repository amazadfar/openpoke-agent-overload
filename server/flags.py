"""Feature flags for the agent-overload fixes (single owner of flag state).

Every behaviour change is behind a flag and every flag defaults to OFF, so the
original OpenPoke behaviour stays reproducible for before/after comparison.

Resolution order (read from ``os.environ`` at call time, so tests can flip flags
without re-importing anything):

1. ``OPENPOKE_FIX_<NAME>=1|0`` — an explicit per-flag override always wins.
2. ``OPENPOKE_FIXES`` profile — ``none`` (default), ``all`` (every fix except the
   ones listed in :data:`EXCLUDED_FROM_ALL`), or a comma-separated list of names.

:func:`validate` checks prerequisites (Appendix A of the design) and is called
on app startup and by the live-eval runner so that a half-enabled feature fails
fast with a clear message instead of misbehaving at runtime.
"""

from __future__ import annotations

import os
from typing import Dict, FrozenSet, List, Tuple

PROFILE_ENV = "OPENPOKE_FIXES"
FLAG_ENV_PREFIX = "OPENPOKE_FIX_"

# (name, task id, mechanism) in implementation order. Order matters only for display.
FLAGS: Tuple[Tuple[str, str, str], ...] = (
    ("WAIT_ENDS_TURN", "1.1", "M3"),
    ("EA_HISTORY_WINDOW", "1.2", "M2"),
    ("KEEP_TOOL_IDS", "1.3", "M2"),
    ("LLM_RETRY", "1.4", "M4"),
    ("GMAIL_READS_OFF_LOOP", "1.5", "M5"),
    ("AGENT_LOCK", "1.6", "M7"),
    ("DISPATCH_DEDUPE", "1.7", "duplicate dispatch"),
    ("SEND_LEDGER", "1.8", "side effects"),
    ("GMAIL_SENDS_OFF_LOOP", "1.9", "M5"),
    ("SEND_GATE", "1.10", "authorization"),
    ("AGENT_REGISTRY", "2.1", "M1/M2 infrastructure"),
    ("AGENT_CARD", "2.2", "M1+M2"),
    ("RESOLVE_ON_WRITE", "2.3", "M1"),
    ("TOPK_ROSTER", "2.4", "M1"),
    ("DORMANCY", "2.5", "M1"),
    ("TURN_BATCHES", "2.6", "M6"),
    ("POLL_BATCH", "2.7", "M4"),
    ("WATCHER_SINGLE_FLIGHT", "2.8", "M4"),
    ("CONCURRENCY_CAP", "2.9", "M4"),
)

FLAG_NAMES: Tuple[str, ...] = tuple(name for name, _, _ in FLAGS)

# Runtime prerequisites (Appendix A). Ordering-only dependencies are not listed.
PREREQUISITES: Dict[str, Tuple[str, ...]] = {
    "DISPATCH_DEDUPE": ("AGENT_LOCK",),
    "GMAIL_SENDS_OFF_LOOP": ("GMAIL_READS_OFF_LOOP", "SEND_LEDGER"),
    "SEND_GATE": ("SEND_LEDGER",),
    "AGENT_REGISTRY": ("KEEP_TOOL_IDS",),
    "AGENT_CARD": ("AGENT_REGISTRY", "EA_HISTORY_WINDOW", "KEEP_TOOL_IDS", "AGENT_LOCK"),
    "RESOLVE_ON_WRITE": ("AGENT_CARD",),
    "TOPK_ROSTER": ("RESOLVE_ON_WRITE",),
    "DORMANCY": ("TOPK_ROSTER",),
    "POLL_BATCH": ("TURN_BATCHES",),
    "WATCHER_SINGLE_FLIGHT": ("GMAIL_READS_OFF_LOOP",),
    "CONCURRENCY_CAP": ("AGENT_LOCK",),
}

# The send gate changes the product UX (every send needs a typed approval code),
# which is an unresolved product decision (U1), so `all` does not turn it on.
EXCLUDED_FROM_ALL: FrozenSet[str] = frozenset({"SEND_GATE"})

# Non-fix switches (documented here so there is one list of every OPENPOKE_* knob).
FAKE_GMAIL_ENV = "OPENPOKE_FAKE_GMAIL"
FAKE_GMAIL_FIXTURE_ENV = "OPENPOKE_FAKE_GMAIL_FIXTURE"
TELEMETRY_ENV = "OPENPOKE_TELEMETRY"
EVAL_CAPTURE_PROMPTS_ENV = "OPENPOKE_EVAL_CAPTURE_PROMPTS"
ALLOW_LIVE_GMAIL_READS_ENV = "OPENPOKE_ALLOW_LIVE_GMAIL_READS"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class FlagConfigError(ValueError):
    """Raised when the flag configuration is inconsistent."""


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in _TRUE


def _normalize_name(raw: str) -> str:
    name = raw.strip().upper()
    if name.startswith(FLAG_ENV_PREFIX):
        name = name[len(FLAG_ENV_PREFIX):]
    return name


def _profile_names() -> Tuple[FrozenSet[str], List[str]]:
    """Return (flags enabled by the profile, unknown names in the profile)."""
    raw = os.environ.get(PROFILE_ENV, "").strip()
    lowered = raw.lower()
    if lowered in ("", "none"):
        return frozenset(), []
    if lowered == "all":
        return frozenset(n for n in FLAG_NAMES if n not in EXCLUDED_FROM_ALL), []
    names = [_normalize_name(part) for part in raw.split(",") if part.strip()]
    unknown = [name for name in names if name not in FLAG_NAMES]
    return frozenset(n for n in names if n in FLAG_NAMES), unknown


def enabled(name: str) -> bool:
    """Return whether fix ``name`` is on. Reads the environment at call time."""
    if name not in FLAG_NAMES:
        raise KeyError(f"Unknown OpenPoke fix flag: {name}")
    explicit = os.environ.get(FLAG_ENV_PREFIX + name)
    if explicit is not None and explicit.strip() != "":
        return _truthy(explicit)
    profile, _ = _profile_names()
    return name in profile


def active_flags() -> List[str]:
    """Return the enabled flags in canonical order."""
    return [name for name in FLAG_NAMES if enabled(name)]


def fake_gmail_enabled() -> bool:
    """True when every Gmail call must go to the in-process fake backend."""
    return _truthy(os.environ.get(FAKE_GMAIL_ENV, ""))


def telemetry_file_enabled() -> bool:
    """The JSONL telemetry sink is on unless OPENPOKE_TELEMETRY=0."""
    raw = os.environ.get(TELEMETRY_ENV, "1").strip().lower()
    return raw not in _FALSE


def eval_capture_prompts_enabled() -> bool:
    """Eval-only full prompt capture (refused unless fake Gmail is on; see validate())."""
    return _truthy(os.environ.get(EVAL_CAPTURE_PROMPTS_ENV, ""))


def validate() -> None:
    """Fail fast on unknown flags, invalid values, missing prerequisites or unsafe combos."""
    problems: List[str] = []

    _, unknown = _profile_names()
    if unknown:
        problems.append(f"{PROFILE_ENV} names unknown flags: {', '.join(sorted(unknown))}")

    for key, value in os.environ.items():
        if not key.startswith(FLAG_ENV_PREFIX):
            continue
        name = key[len(FLAG_ENV_PREFIX):]
        if name not in FLAG_NAMES:
            problems.append(f"{key} is not a known flag")
        elif value.strip() and value.strip().lower() not in _TRUE | _FALSE:
            problems.append(f"{key}={value!r} is not a boolean (use 1 or 0)")

    for name, requires in PREREQUISITES.items():
        if not enabled(name):
            continue
        missing = [req for req in requires if not enabled(req)]
        if missing:
            problems.append(f"{name} requires {', '.join(missing)}")

    if eval_capture_prompts_enabled() and not fake_gmail_enabled():
        problems.append(
            f"{EVAL_CAPTURE_PROMPTS_ENV}=1 writes full prompts to disk and is only allowed with {FAKE_GMAIL_ENV}=1"
        )

    if problems:
        raise FlagConfigError("Invalid OpenPoke flag configuration: " + "; ".join(problems))

    # Constants are validated with the flags so one call covers all configuration.
    from . import overload_config

    overload_config.validate()


__all__ = [
    "FLAGS",
    "FLAG_NAMES",
    "PREREQUISITES",
    "EXCLUDED_FROM_ALL",
    "FlagConfigError",
    "active_flags",
    "enabled",
    "eval_capture_prompts_enabled",
    "fake_gmail_enabled",
    "telemetry_file_enabled",
    "validate",
]
