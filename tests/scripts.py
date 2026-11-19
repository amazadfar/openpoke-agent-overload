"""Reusable scripted-model behaviours for L2 tests.

These encode *model behaviour by hand* (for example, a model that spins on
`wait`, or a model that rewords an agent name). L2 therefore tests mechanisms,
not what a real model would do; only the L3 evals measure model behaviour.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, Optional

from server.testing.scripted_llm import LLMCall, final, respond, tool_call


def is_agent_turn(call: LLMCall) -> bool:
    return "<new_agent_message>" in call.first_user_content


def round_index(call: LLMCall) -> int:
    """How many assistant rounds already happened in this turn."""
    return sum(1 for m in call.messages if m.get("role") == "assistant")


def ia_relay(reply_prefix: str = "Update") -> Callable[[LLMCall], Dict]:
    """A well-behaved IA: on agent results, tell the user once, then end the turn."""

    def handler(call: LLMCall) -> Dict:
        if is_agent_turn(call) and round_index(call) == 0:
            return respond(tool_call("send_message_to_user", message=f"{reply_prefix}: results received"))
        return final("")

    return handler


def ia_waiter(reply_prefix: str = "Update") -> Callable[[LLMCall], Dict]:
    """The observed real-run behaviour (S9 trace): reply/dispatch, then keep calling `wait`."""

    def handler(call: LLMCall) -> Dict:
        if is_agent_turn(call) and round_index(call) == 0:
            return respond(tool_call("send_message_to_user", message=f"{reply_prefix}: results received"))
        return respond(tool_call("wait", reason="Waiting for the agent"))

    return handler


def ea_final(text: str) -> Dict:
    return final(text)


def last_tool_result(call: LLMCall) -> Optional[dict]:
    results = call.tool_results()
    return results[-1] if results else None


def tool_args(response: Dict) -> Dict:
    return json.loads(response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
