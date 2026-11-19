"""Interaction agent helpers for prompt construction."""

from html import escape
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ... import flags
from ...services import telemetry
from ...services.execution import get_agent_roster
from .prompt_extensions import render_ia

_prompt_path = Path(__file__).parent / "system_prompt.md"
SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8").strip()


# Load and return the pre-defined system prompt from markdown file
def build_system_prompt() -> str:
    """Return the static system prompt for the interaction agent (+ flag-scoped snippets)."""
    return SYSTEM_PROMPT + render_ia()


# Build structured message with conversation history, active agents, and current turn
def prepare_message_with_history(
    latest_text: str,
    transcript: str,
    message_type: str = "user",
) -> List[Dict[str, str]]:
    """Compose a message that bundles history, roster, and the latest turn."""
    sections: List[str] = []

    history = _render_conversation_history(transcript)
    roster_body, roster_total, roster_shown = _render_roster(latest_text)
    roster = f"<active_agents>\n{roster_body}\n</active_agents>"
    turn = _render_current_turn(latest_text, message_type)

    sections.append(history)
    sections.append(roster)
    sections.append(turn)

    telemetry.emit(
        "prompt_parts",
        role="interaction",
        system_chars=len(build_system_prompt()),
        history_chars=len(history),
        roster_chars=len(roster),
        turn_chars=len(turn),
        roster_total=roster_total,
        roster_shown=roster_shown,
    )

    content = "\n\n".join(sections)
    return [{"role": "user", "content": content}]


# Format conversation transcript into XML tags for LLM context
def _render_conversation_history(transcript: str) -> str:
    history = transcript.strip()
    if not history:
        history = "None"
    return f"<conversation_history>\n{history}\n</conversation_history>"


def _render_roster(latest_text: str) -> Tuple[str, int, int]:
    """Return (roster block body, agents total, agents shown)."""
    if flags.enabled("TOPK_ROSTER"):
        return _render_top_k(latest_text)
    roster = get_agent_roster()
    roster.load()
    agents = roster.get_agents()
    return _render_active_agents_from(agents), len(agents), len(agents)


def _ago(value: Optional[str]) -> str:
    from ...services.agents.registry import parse_time
    from ...utils import clock

    when = parse_time(value)
    if when is None:
        return "never"
    seconds = max(0, int((clock.now() - when).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _render_top_k(latest_text: str) -> Tuple[str, int, int]:
    """2.4 (M1): K relevant agents with a one-line purpose instead of every name ever created."""
    from ...overload_config import TOPK_PURPOSE_CHARS
    from ...services.agents.ranking import select
    from ...services.agents.registry import active_trigger_owners, get_agent_registry

    roster = get_agent_roster()
    roster.load()  # migration / roster sync
    registry = get_agent_registry()
    rows = registry.list_all()
    if not rows:
        return "None", 0, 0
    visible = rows
    if flags.enabled("DORMANCY"):
        owners = active_trigger_owners()
        visible = [row for row in rows if not registry.is_dormant(row, owners)]
    aliases, emails = registry.all_aliases(), registry.all_emails()
    documents = {
        row.id: " ".join(filter(None, [row.name, *aliases.get(row.id, []), row.purpose, row.latest_request,
                                       " ".join(emails.get(row.id, [])), row.last_outcome]))
        for row in visible
    }
    shown = select(latest_text, visible, documents)
    limit = int(TOPK_PURPOSE_CHARS.get())
    lines = []
    for row in shown:
        purpose = (row.latest_request or row.purpose or "")[:limit]
        lines.append(
            f'<agent name="{escape(row.name, quote=True)}" last_active="{_ago(row.last_active_at)}" runs="{row.runs}">'
            f"{escape(purpose, quote=False)}</agent>"
        )
    hidden = len(rows) - len(shown)
    if hidden > 0:
        lines.append(
            f'<more_agents count="{hidden}">Not listed. Use a descriptive agent_name including the counterparty '
            "or thread; existing agents are matched automatically.</more_agents>"
        )
    return "\n".join(lines), len(rows), len(shown)


# Format currently active execution agents into XML tags for LLM awareness
def _render_active_agents() -> str:
    roster = get_agent_roster()
    roster.load()
    return _render_active_agents_from(roster.get_agents())


def _render_active_agents_from(agents: List[str]) -> str:
    if not agents:
        return "None"

    rendered: List[str] = []
    for agent_name in agents:
        name = escape(agent_name or "agent", quote=True)
        rendered.append(f'<agent name="{name}" />')

    return "\n".join(rendered)


# Wrap the current message in appropriate XML tags based on sender type
def _render_current_turn(latest_text: str, message_type: str) -> str:
    tag = "new_agent_message" if message_type == "agent" else "new_user_message"
    body = latest_text.strip()
    return f"<{tag}>\n{body}\n</{tag}>"
