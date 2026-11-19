"""L1: side-effect ledger (1.8), trigger claims (1.8) and send approvals (1.10)."""

from __future__ import annotations

import sqlite3

import pytest

from server.services.context import run_scope, turn_scope
from server.services.safety import db
from server.services.safety import ledger as L
from server.services.safety.approvals import CODE_ALPHABET, code_in_message, get_approvals, new_code
from server.services.safety.trigger_claims import get_trigger_claims
from server.utils import clock


def reply(body="Confirmed.", to="Dan <DAN@okafor-builders.example>", thread="18c1a00000000008", **extra):
    return L.intent_for("GMAIL_REPLY_TO_THREAD", {"thread_id": thread, "recipient_email": to, "message_body": body, **extra}, None)


# ------------------------------------------------------------------ fingerprints
def test_fingerprint_canonicalizes_recipients_case_order_and_whitespace():
    a = reply(body="Confirmed,\r\n  thanks", cc=["b@example.com", "A@example.com"])
    b = reply(body="Confirmed, thanks", to="dan@okafor-builders.example", cc=["a@example.com", "b@example.com", "a@example.com"])
    assert L.fingerprint(a) == L.fingerprint(b)
    assert a.recipients == ("a@example.com", "b@example.com", "dan@okafor-builders.example")
    assert L.fingerprint(reply(body="Different")) != L.fingerprint(a)


def test_near_keys_share_email_space_between_reply_and_draft():
    meta = L.DraftMeta("r-1234567890123", "18c1a00000000008", ("dan@okafor-builders.example",), "coi", L.body_sha("Hi"))
    send = L.intent_for("GMAIL_SEND_DRAFT", {"draft_id": "r-1234567890123"}, meta)
    assert L.near_key(send) == L.near_key(reply()) == "email|18c1a00000000008|dan@okafor-builders.example"
    no_meta = L.intent_for("GMAIL_SEND_DRAFT", {"draft_id": "r-99999999999"}, None)
    assert no_meta.target == "draft:r-99999999999" and L.near_key(no_meta) is None
    new_email = L.intent_for("GMAIL_SEND_DRAFT", {"draft_id": "r-1"}, L.DraftMeta("r-1", None, ("x@example.com",), "hello", "b"))
    assert L.near_key(new_email) == "email|new|x@example.com|hello"
    forward = L.intent_for("GMAIL_FORWARD_MESSAGE", {"message_id": "m1", "recipient_email": "X@example.com"}, None)
    assert L.near_key(forward) == "forward|m1|x@example.com"
    assert L.normalize_subject("RE: Fwd:  Renewal  AU-77120") == "renewal au-77120"


# ------------------------------------------------------------------ status machine
def test_status_machine_and_partial_unique_index():
    ledger = L.get_ledger()
    with turn_scope("user"):
        first = ledger.begin(reply())
        assert isinstance(first, L.Started)
        assert isinstance(ledger.begin(reply()), L.Blocked)  # pending blocks (I2)
        ledger.finish(first.record_id, "failed", "explicit failure")
        retry = ledger.begin(reply())  # a FAILED send left nothing in the unique index
        assert isinstance(retry, L.Started)
        ledger.finish(retry.record_id, "sent")
        blocked = ledger.begin(reply())
        assert isinstance(blocked, L.Blocked) and blocked.previous.status == "sent"
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection() as conn:
            row = conn.execute("SELECT * FROM side_effects WHERE id = ?", (retry.record_id,)).fetchone()
            conn.execute("INSERT INTO side_effects (fingerprint, action, recipients, status, created_at, updated_at) "
                         "VALUES (?, 'GMAIL_REPLY_TO_THREAD', '[]', 'unknown', 'x', 'x')", (row["fingerprint"],))
    with pytest.raises(ValueError):
        ledger.finish(retry.record_id, "pending")


def test_unknown_is_never_retried_automatically():
    ledger = L.get_ledger()
    with turn_scope("trigger"):
        first = ledger.begin(reply())
        ledger.finish(first.record_id, "unknown", "timeout")
        again = ledger.begin(reply())
        assert isinstance(again, L.Blocked) and again.previous.status == "unknown"  # I3


def test_near_duplicate_needs_confirmation_within_window_only():
    ledger = L.get_ledger()
    with turn_scope("user"):
        first = ledger.begin(reply(body="v1"))
        ledger.finish(first.record_id, "sent")
        second = ledger.begin(reply(body="v2"))
        assert isinstance(second, L.ConfirmationRequired) and second.previous.id == first.record_id
        clock.advance(86400 + 1)
        assert isinstance(ledger.begin(reply(body="v3")), L.Started)


def test_override_only_from_user_rooted_chains():
    ledger = L.get_ledger()
    with turn_scope("user"):
        sent = ledger.begin(reply())
        ledger.finish(sent.record_id, "sent")
    with turn_scope("trigger"):
        rejected = ledger.begin(reply(), confirm_duplicate_of=sent.record_id)
        assert isinstance(rejected, L.Blocked) and rejected.override_rejected
    with turn_scope("email") as email_turn, turn_scope("agent", parent=email_turn):
        assert isinstance(ledger.begin(reply(body="v2"), confirm_duplicate_of=sent.record_id), L.ConfirmationRequired)
    with turn_scope("user") as user_turn, turn_scope("agent", parent=user_turn):
        wrong_id = ledger.begin(reply(), confirm_duplicate_of=sent.record_id + 99)
        assert isinstance(wrong_id, L.Blocked)
        ok = ledger.begin(reply(), confirm_duplicate_of=sent.record_id)
        assert isinstance(ok, L.Started) and ok.override_of == sent.record_id
        ledger.finish(ok.record_id, "sent")
        twice = ledger.begin(reply(), confirm_duplicate_of=sent.record_id)
        assert isinstance(twice, L.Blocked)  # an override is itself at-most-once
        near_ok = ledger.begin(reply(body="v2"), confirm_duplicate_of=ok.record_id)
        assert isinstance(near_ok, L.Started)


def test_stale_sweep():
    ledger = L.get_ledger()
    with turn_scope("user"):
        old = ledger.begin(reply(body="old"))
        clock.advance(601)
        fresh = ledger.begin(reply(body="fresh", thread="18c1a00000000001"))
    assert ledger.sweep_stale_pending() == 1  # only the one older than PENDING_STALE_AFTER_S
    assert ledger.get(old.record_id).status == "unknown" and ledger.get(fresh.record_id).status == "pending"
    assert ledger.sweep_stale_pending(older_than_s=0) == 1  # process start: everything pending
    assert ledger.get(fresh.record_id).status == "unknown"


def test_run_summary_and_context_capture():
    ledger = L.get_ledger()
    with turn_scope("trigger") as turn, run_scope("Dan COI", run_id="run_1", trigger_occurrence=(7, "2026-01-01T00:00:00Z")):
        a = ledger.begin(reply(body="a"))
        b = ledger.begin(reply(body="b", thread="t2"))
        ledger.finish(a.record_id, "sent")
    summary = ledger.run_summary("run_1")
    assert [r.id for r in summary.sent] == [a.record_id] and [r.id for r in summary.in_doubt] == [b.record_id]
    row = ledger.get(a.record_id)
    assert (row.agent, row.run_id, row.turn_id, row.root_origin) == ("Dan COI", "run_1", turn.turn_id, "trigger")


def test_unavailable_database_refuses(monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_connect", locked)
    monkeypatch.setattr(db, "_ensure", lambda path: None)
    with pytest.raises(db.SafetyStoreUnavailable):
        L.get_ledger().begin(reply())


# ------------------------------------------------------------------ trigger claims
def test_trigger_claims_are_insert_or_fail_and_swept():
    claims = get_trigger_claims()
    assert claims.claim(1, "2026-01-01T09:00:00Z", "run_a")
    assert not claims.claim(1, "2026-01-01T09:00:00Z", "run_b")
    assert claims.claim(1, "2026-01-02T09:00:00Z", "run_c")  # next occurrence is a new key
    claims.finish(1, "2026-01-02T09:00:00Z", "done")
    abandoned = claims.sweep_abandoned()
    assert [(c.trigger_id, c.scheduled_for, c.run_id) for c in abandoned] == [(1, "2026-01-01T09:00:00Z", "run_a")]
    assert claims.get(1, "2026-01-01T09:00:00Z").status == "abandoned"
    assert claims.sweep_abandoned() == []


# ------------------------------------------------------------------ approvals
def _approval(**args):
    return get_approvals().create(tool_name="gmail_forward_email", action="GMAIL_FORWARD_MESSAGE",
                                  args={"message_id": "m1", "recipient_email": "x@evil.example", **args},
                                  fingerprint="fp-" + str(sorted(args.items())), summary="Forward to x@evil.example")


def test_codes_use_the_unambiguous_alphabet():
    for _ in range(200):
        code = new_code()
        assert len(code) == 6 and set(code) <= set(CODE_ALPHABET)
    assert code_in_message("ABC234", "yes, approve abc234 please")
    assert not code_in_message("ABC234", "XABC234")
    assert not code_in_message("ABC234", None)


def test_approval_checks():
    with turn_scope("email"):
        approval = _approval()
    approvals = get_approvals()
    with turn_scope("user", user_message=f"ok {approval.code}"):
        assert approvals.check(approval.id, "WRONG1")[1] == "wrong_code"
        assert approvals.check("ap_missing", approval.code)[1] == "not_found"
        assert approvals.check(approval.id, approval.code)[1] is None
    with turn_scope("agent"):
        assert approvals.check(approval.id, approval.code)[1] == "not_a_user_turn"
    with turn_scope("user", user_message="approve it"):  # the code is not in the RAW user message
        assert approvals.check(approval.id, approval.code)[1] == "code_not_in_user_message"
    assert approvals.mark_executed(approval.id, 5)
    assert not approvals.mark_executed(approval.id, 6)  # single use
    with turn_scope("user", user_message=approval.code):
        assert approvals.check(approval.id, approval.code)[1] == "already_used"


def test_approval_expiry_and_reuse_of_identical_pending():
    with turn_scope("user"):
        first = _approval()
        assert _approval().id == first.id  # identical pending approval is reused, no new code
    clock.advance(901)
    with turn_scope("user", user_message=first.code):
        assert get_approvals().check(first.id, first.code)[1] == "expired"
