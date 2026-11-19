from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from ..config import get_settings
from ..logging_config import logger

OpenRouterBaseURL = "https://openrouter.ai/api/v1"


class OpenRouterError(RuntimeError):
    """Raised when the OpenRouter API returns an error response."""


def _headers(*, api_key: Optional[str] = None) -> Dict[str, str]:
    settings = get_settings()
    key = (api_key or settings.openrouter_api_key or "").strip()
    if not key:
        raise OpenRouterError("Missing OpenRouter API key")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    return headers


def _build_messages(messages: List[Dict[str, str]], system: Optional[str]) -> List[Dict[str, str]]:
    if system:
        return [{"role": "system", "content": system}, *messages]
    return messages


def _handle_response_error(exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    detail: str
    try:
        payload = response.json()
        detail = payload.get("error") or payload.get("message") or json.dumps(payload)
    except Exception:
        detail = response.text
    raise OpenRouterError(f"OpenRouter request failed ({response.status_code}): {detail}") from exc


async def request_chat_completion(
    *,
    model: str,
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    api_key: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    base_url: str = OpenRouterBaseURL,
) -> Dict[str, Any]:
    """Request a chat completion and return the raw JSON payload."""

    payload: Dict[str, object] = {
        "model": model,
        "messages": _build_messages(messages, system),
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    url = f"{base_url.rstrip('/')}/chat/completions"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                url,
                headers=_headers(api_key=api_key),
                json=payload,
                timeout=60.0,  # Set reasonable timeout instead of None
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("OpenRouter request failed for model %s (HTTP %s)", model, exc.response.status_code)
            _handle_response_error(exc)
        except httpx.HTTPError as exc:
            logger.error("OpenRouter request failed for model %s (%s)", model, type(exc).__name__)
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

        result = response.json()
        if isinstance(result, dict) and result.get("error"):
            error = result["error"]
            code = error.get("code") if isinstance(error, dict) else None
            logger.error(
                "OpenRouter returned an error for model %s (HTTP %s, error code %s)",
                model,
                response.status_code,
                code if isinstance(code, int) else "unknown",
            )
            raise OpenRouterError("OpenRouter returned an error in the completion response")
        return result


__all__ = ["OpenRouterError", "request_chat_completion", "OpenRouterBaseURL"]
