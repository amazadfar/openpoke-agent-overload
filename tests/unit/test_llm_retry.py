"""L1: deadline-aware LLM retries (1.4, flag LLM_RETRY) with httpx.MockTransport and a fake sleep."""

from __future__ import annotations

import random

import httpx
import pytest

from helpers import run_async
from server.openrouter_client import client as or_client
from server.services import telemetry
from server.services.context import run_scope
from server.utils import clock

OK = {"choices": [{"message": {"content": "hi"}}]}


class Script:
    """Serves a list of responses (or exceptions) in order and records requests."""

    def __init__(self, items):
        self.items = list(items)
        self.requests = 0

    def __call__(self, request):
        self.requests += 1
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def transport(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)  # time passes for deadline checks, instantly

    monkeypatch.setattr(or_client, "_sleep", fake_sleep)
    monkeypatch.setattr(or_client, "_rng", random.Random(7))

    def install(items):
        script = Script(items)
        mock = httpx.MockTransport(script)
        monkeypatch.setattr(or_client, "_http_client", lambda: httpx.AsyncClient(transport=mock))
        return script

    install.sleeps = sleeps
    return install


def call():
    return run_async(lambda: or_client.request_chat_completion(model="m", messages=[{"role": "user", "content": "x"}], role="execution"))


def test_flag_off_never_retries(flags_off, transport):
    script = transport([httpx.Response(429, json={"error": "slow down"}), httpx.Response(200, json=OK)])
    with pytest.raises(or_client.OpenRouterError, match="429"):
        call()
    assert script.requests == 1


def test_429_then_200_succeeds(set_flags, transport):
    set_flags("LLM_RETRY")
    script = transport([httpx.Response(429, json={"error": "slow down"}), httpx.Response(200, json=OK)])
    with telemetry.capture() as events:
        assert call() == OK
    attempts = [(e["attempt"], e["retry_reason"], e["ok"]) for e in events if e["event"] == "llm_call"]
    assert attempts == [(1, None, False), (2, "http_429", True)]
    assert script.requests == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_4xx_are_never_retried(set_flags, transport, status):
    set_flags("LLM_RETRY")
    script = transport([httpx.Response(status, json={"error": "bad"}), httpx.Response(200, json=OK)])
    with pytest.raises(or_client.OpenRouterError):
        call()
    assert script.requests == 1


@pytest.mark.parametrize("failure,reason", [
    (httpx.Response(503, json={"error": "x"}), "http_503"),
    (httpx.ConnectError("refused"), "ConnectError"),
    (httpx.ReadTimeout("slow"), "ReadTimeout"),
    (httpx.Response(200, json={"error": {"code": 502, "message": "upstream"}}), "body_502"),
])
def test_transient_failures_are_retried(set_flags, transport, failure, reason):
    set_flags("LLM_RETRY")
    transport([failure, httpx.Response(200, json=OK)])
    with telemetry.capture() as events:
        assert call() == OK
    assert [e["retry_reason"] for e in events if e["event"] == "llm_call"] == [None, reason]


def test_retry_after_is_honoured_and_capped(set_flags, transport):
    set_flags("LLM_RETRY")
    transport([httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "x"}),
               httpx.Response(429, headers={"Retry-After": "999"}, json={"error": "x"}),
               httpx.Response(200, json=OK)])
    assert call() == OK
    assert transport.sleeps == [7.0, 20.0]


def test_attempts_cap_holds(set_flags, transport):
    set_flags("LLM_RETRY")
    script = transport([httpx.Response(503, json={"error": "down"})])
    with pytest.raises(or_client.OpenRouterError, match="503"):
        call()
    assert script.requests == 3 and len(transport.sleeps) == 2


def test_jitter_stays_within_bounds(set_flags, monkeypatch):
    set_flags("LLM_RETRY")
    for seed in range(200):
        monkeypatch.setattr(or_client, "_rng", random.Random(seed))
        for attempt in (1, 2, 3, 6):
            delay = or_client._backoff(attempt, None)
            assert 0.0 <= delay <= min(20.0, 1.0 * 2 ** (attempt - 1))


def test_deadline_stops_retries(set_flags, transport):
    set_flags("LLM_RETRY")
    script = transport([httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "x"}), httpx.Response(200, json=OK)])

    async def main():
        # 12 s left: 5 s backoff + 10 s minimum call budget does not fit.
        with run_scope("A", deadline_monotonic=clock.monotonic() + 12):
            return await or_client.request_chat_completion(model="m", messages=[], role="execution")

    with telemetry.capture() as events:
        with pytest.raises(or_client.OpenRouterError):
            run_async(main)
    assert script.requests == 1
    assert [e for e in events if e["event"] == "llm_retry_skipped"][0]["reason"] == "deadline"


def test_attempt_timeout_follows_deadline(set_flags):
    set_flags("LLM_RETRY")
    now = clock.monotonic()
    assert or_client._attempt_timeout(None) == 60.0
    assert or_client._attempt_timeout(now + 30) == pytest.approx(29.0, abs=0.1)
    assert or_client._attempt_timeout(now + 2) == 5.0  # floor
    assert or_client._attempt_timeout(now + 500) == 60.0
