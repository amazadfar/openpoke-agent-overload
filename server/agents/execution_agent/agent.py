"""Execution Agent implementation."""

import json
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from ... import flags
from ...services.execution import get_execution_agent_logs
from ...services.execution.log_store import render_entry
from ...logging_config import logger

CARD_HEADING = "# Agent card (maintained by the system; may be incomplete)"
TRUNCATION_MARKER = " …[truncated {n} chars]"


# Load system prompt template from file
_prompt_path = Path(__file__).parent / "system_prompt.md"
if _prompt_path.exists():
    SYSTEM_PROMPT_TEMPLATE = _prompt_path.read_text(encoding="utf-8").strip()
else:
    # Placeholder template - you'll replace this with actual instructions
    SYSTEM_PROMPT_TEMPLATE = """You are an execution agent responsible for completing specific tasks using available tools.

Agent Name: {agent_name}
Purpose: {agent_purpose}

Instructions:
[TO BE FILLED IN BY USER]

You have access to Gmail tools to help complete your tasks. When given instructions:
1. Analyze what needs to be done
2. Use the appropriate tools to complete the task
3. Provide clear status updates on your actions

Be thorough, accurate, and efficient in your execution."""


class ExecutionAgent:
    """Manages state and history for an execution agent."""

    # Initialize execution agent with name, conversation limits, and log store access
    def __init__(
        self,
        name: str,
        conversation_limit: Optional[int] = None,
        max_history_chars: Optional[int] = None,
        card_text: Optional[str] = None,
    ):
        """
        Initialize an execution agent.

        Args:
            name: Human-readable agent name (e.g., 'conversation with keith')
            conversation_limit: Optional limit on past conversations to include (None = all)
            max_history_chars: Optional cap on rendered history (1.2; None = no cap, the baseline)
            card_text: Optional rendered agent card placed before the history (2.2)
        """
        self.name = name
        self.conversation_limit = conversation_limit
        self.max_history_chars = max_history_chars
        self.card_text = card_text
        self._log_store = get_execution_agent_logs()
        # Sizes of the last built prompt (telemetry only; never content).
        self.last_prompt_parts: Dict[str, Any] = {}

    # Generate system prompt template with agent name and purpose derived from name
    def build_system_prompt(self) -> str:
        """Build the system prompt for this agent."""
        agent_purpose = f"Handle tasks related to: {self.name}"

        # Lazy import: interaction_agent imports this module (avoid an import cycle).
        from ..interaction_agent.prompt_extensions import render_ea

        return SYSTEM_PROMPT_TEMPLATE.format(
            agent_name=self.name,
            agent_purpose=agent_purpose
        ) + render_ea()

    # Combine base system prompt with conversation history, applying conversation limits
    def build_system_prompt_with_history(self) -> str:
        """
        Build system prompt including agent history.

        Returns:
            System prompt with embedded history transcript
        """
        base_prompt = self.build_system_prompt()
        if self.card_text:
            base_prompt = f"{base_prompt}\n\n{CARD_HEADING}\n{self.card_text}"
        if self.max_history_chars is not None:
            return self._build_windowed(base_prompt)

        # Load history transcript
        transcript = self._log_store.load_transcript(self.name)
        self.last_prompt_parts = {
            "template_chars": len(base_prompt),
            "card_chars": 0,
            "history_chars": 0,
            "history_requests": 0,
        }

        if transcript:
            # Apply conversation limit if needed
            if self.conversation_limit and self.conversation_limit > 0:
                # Parse entries and limit them
                lines = transcript.split('\n')
                request_count = sum(1 for line in lines if '<agent_request' in line)

                if request_count > self.conversation_limit:
                    # Find where to cut
                    kept_requests = 0
                    cutoff_index = len(lines)
                    for i in range(len(lines) - 1, -1, -1):
                        if '<agent_request' in lines[i]:
                            kept_requests += 1
                            if kept_requests == self.conversation_limit:
                                cutoff_index = i
                                break
                    transcript = '\n'.join(lines[cutoff_index:])

            self.last_prompt_parts["history_chars"] = len(transcript)
            self.last_prompt_parts["history_requests"] = transcript.count("<agent_request")
            return f"{base_prompt}\n\n# Execution History\n\n{transcript}"

        return base_prompt

    def _build_windowed(self, base_prompt: str) -> str:
        """1.2 (M2): keep the last K requests' entries, then drop oldest whole entries to fit the char cap.

        K counts the current request (it is journaled before the prompt is built),
        which is the existing semantics of ``conversation_limit``.
        """
        card_chars = len(self.card_text or "")
        entries = list(self._log_store.iter_entries(self.name))
        if self.conversation_limit and self.conversation_limit > 0:
            request_positions = [i for i, (tag, _, _) in enumerate(entries) if tag == "agent_request"]
            if len(request_positions) > self.conversation_limit:
                entries = entries[request_positions[-self.conversation_limit]:]

        rendered = [render_entry(*entry) for entry in entries]
        rendered = _fit_to_chars(rendered, entries, int(self.max_history_chars or 0))
        transcript = "\n".join(rendered)
        self.last_prompt_parts = {
            "template_chars": len(base_prompt) - (len(CARD_HEADING) + 3 + card_chars if card_chars else 0),
            "card_chars": card_chars,
            "history_chars": len(transcript),
            "history_requests": transcript.count("<agent_request"),
        }
        if not transcript:
            return base_prompt
        return f"{base_prompt}\n\n# Execution History\n\n{transcript}"

    # Format current instruction as user message for LLM consumption
    def build_messages_for_llm(self, current_instruction: str) -> List[Dict[str, str]]:
        """
        Build message array for LLM call.

        Args:
            current_instruction: Current instruction from interaction agent

        Returns:
            List of messages in OpenRouter format
        """
        return [
            {"role": "user", "content": current_instruction}
        ]

    # Log the agent's final response to the execution log store
    def record_response(self, response: str) -> None:
        """Record agent's response to the log."""
        self._log_store.record_agent_response(self.name, response)

    # Log tool invocation and results with truncated content for readability
    def record_tool_execution(
        self,
        tool_name: str,
        arguments: str,
        result: str,
        *,
        raw_arguments: Any = None,
        raw_result: Any = None,
    ):
        """Record tool execution details.

        With KEEP_TOOL_IDS (1.3) the structured identifiers (thread, message,
        draft ids and counterparty emails) are also journaled as ``<tool_ids>``,
        because the 200/500-char truncation below drops exactly the ids that
        follow-ups need. Returns the extracted ids (empty when the flag is off).
        """
        self._log_store.record_action(self.name, f"Calling {tool_name} with: {arguments[:200]}")
        # Record the tool response
        self._log_store.record_tool_response(self.name, tool_name, result[:500])
        if not flags.enabled("KEEP_TOOL_IDS"):
            return None
        from ...services.agents.ids import email_refs, extract_from_tool_call

        extracted = extract_from_tool_call(tool_name, raw_arguments or {}, raw_result)
        if not extracted.is_empty():
            entry = extracted.to_journal(tool_name)
            refs = email_refs(raw_result)
            if refs:
                entry["refs"] = refs
            self._log_store.record_entry(self.name, "tool_ids", json.dumps(entry, sort_keys=True))
        return extracted


def _fit_to_chars(rendered: List[str], entries: List[Tuple[str, str, str]], cap: int) -> List[str]:
    """Drop oldest whole entries until the joined history fits ``cap``; never drop the newest.

    A single newest entry larger than the cap is truncated (payload only, so the
    tag stays well-formed) with an explicit marker.
    """
    if cap <= 0 or not rendered:
        return rendered
    total = sum(len(r) for r in rendered) + len(rendered) - 1
    start = 0
    while total > cap and len(rendered) - start > 1:
        total -= len(rendered[start]) + 1
        start += 1
    kept = rendered[start:]
    if len(kept) == 1 and len(kept[0]) > cap:
        tag, timestamp, payload = entries[-1]
        overhead = len(render_entry(tag, timestamp, "")) + len(TRUNCATION_MARKER.format(n=len(payload)))
        keep = max(0, cap - overhead)
        marker = TRUNCATION_MARKER.format(n=len(payload) - keep)
        kept = [render_entry(tag, timestamp, payload[:keep] + marker)]
    return kept
