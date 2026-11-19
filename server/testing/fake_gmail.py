"""In-process fake Gmail backend (tests, parity runs and evals; never production).

Enabled by ``OPENPOKE_FAKE_GMAIL=1``: ``services/gmail/client.py::execute_gmail_tool``
is the single switch and routes every action here, so Composio is never touched.

Response envelope mirrors the Composio SDK's ``ToolExecuteResponse``
(``{"data", "error", "log_id", "session_info", "successful"}``, verified from the
installed SDK). The *contents* of ``data`` follow the Gmail API resources
(draft, message, list) because Composio's per-tool payloads have not been
verified against a live account (U13). Fetch output is designed to parse through
the real ``parse_gmail_fetch_response``; L1 tests assert that.

Query language: a parser for Gmail's syntax (implicit AND, uppercase OR,
``{a b}``, parentheses, ``-`` negation, quoted phrases) and the operators the
fixture can answer (see ``parse_query``). Unknown operators are ignored and counted.

Self-contained on purpose (stdlib only; optional server imports): the parity
harness loads this file by path to drive the *baseline* tree too.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

UTC = timezone.utc

EXTERNAL_SEND_ACTIONS = frozenset({"GMAIL_SEND_DRAFT", "GMAIL_REPLY_TO_THREAD", "GMAIL_FORWARD_MESSAGE"})
SUPPORTED_ACTIONS = frozenset(
    {
        "GMAIL_FETCH_EMAILS",
        "GMAIL_CREATE_EMAIL_DRAFT",
        "GMAIL_SEND_DRAFT",
        "GMAIL_REPLY_TO_THREAD",
        "GMAIL_FORWARD_MESSAGE",
        "GMAIL_DELETE_DRAFT",
        "GMAIL_LIST_DRAFTS",
        "GMAIL_GET_CONTACTS",
        "GMAIL_GET_PEOPLE",
        "GMAIL_SEARCH_PEOPLE",
        "GMAIL_GET_PROFILE",
    }
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_AGE_RE = re.compile(r"(\d+)\s*([dhm])")


def _default_now() -> datetime:
    try:  # use the shared test clock when running inside the current tree
        from server.utils import clock  # type: ignore

        return clock.now()
    except Exception:  # baseline tree (parity) or standalone use
        return datetime.now(UTC)


def _emit(event: str, **fields: Any) -> None:
    try:
        from server.services import telemetry  # type: ignore

        telemetry.emit(event, **fields)
    except Exception:
        pass


def _context_fields() -> Dict[str, Any]:
    """Run/turn correlation for eval gold labelling (empty on the baseline tree)."""
    try:
        from server.services.context import current_run, current_turn  # type: ignore

        run = current_run()
        return {"run_id": run.run_id if run else None, "agent": run.agent if run else None,
                "turn_id": current_turn().turn_id}
    except Exception:
        return {}


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_age(age: Any) -> timedelta:
    """'3d', '2h 30m', '-1d4h' -> timedelta (sign ignored: always 'ago')."""
    if age is None:
        return timedelta(0)
    if isinstance(age, (int, float)):
        return timedelta(hours=float(age))
    total = timedelta(0)
    for amount, unit in _AGE_RE.findall(str(age)):
        n = int(amount)
        total += {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
    return total


def emails_in(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        found: List[str] = []
        for item in value:
            found.extend(emails_in(item))
        return found
    return [match.lower() for match in _EMAIL_RE.findall(str(value))]


def normalize_body(text: Optional[str]) -> str:
    return " ".join((text or "").replace("\r\n", "\n").split())


@dataclass
class FakeMessage:
    id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    body: str
    timestamp: datetime
    labels: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    attachments: List[Dict[str, Any]] = field(default_factory=list)

    def searchable(self) -> str:
        return " ".join([self.subject, self.body, self.sender, self.to, " ".join(self.cc)]).lower()


@dataclass
class _Failure:
    exception: Optional[Exception]
    unsuccessful: bool
    after_effect: bool
    remaining: int


class QueryResult:
    def __init__(self, messages: List[FakeMessage], unsupported: List[str]) -> None:
        self.messages = messages
        self.unsupported = unsupported


class FakeGmail:
    """Deterministic in-memory Gmail account."""

    def __init__(
        self,
        fixture: Optional[Dict[str, Any]] = None,
        *,
        latency_s: float = 0.0,
        mode: str = "sync",
        seed: str = "openpoke-fake-gmail",
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if mode not in ("sync", "instant"):
            raise ValueError("mode must be 'sync' or 'instant'")
        self.mode = mode
        self._default_latency = float(latency_s)
        self._latency: Dict[str, float] = {}
        self._seed = seed
        self._counter = 0
        self._now = now_fn or _default_now
        self._lock = threading.RLock()
        self._log: List[Dict[str, Any]] = []
        self._failures: Dict[str, List[_Failure]] = {}
        self.messages: Dict[str, FakeMessage] = {}
        self.drafts: Dict[str, str] = {}  # draft_id -> message_id
        self.account_email = "me@example.com"
        self.contacts: List[Dict[str, str]] = []
        self.unsupported_query_operators = 0
        if fixture:
            self.load_fixture(fixture)

    # ------------------------------------------------------------------ setup
    def load_fixture(self, fixture: Dict[str, Any]) -> None:
        with self._lock:
            now = self._now()
            self.account_email = fixture.get("account_email", self.account_email)
            self.contacts = list(fixture.get("contacts", []))
            for thread in fixture.get("threads", []):
                thread_id = thread.get("thread_id") or self._new_hex("thread")
                for message in thread.get("messages", []):
                    msg = FakeMessage(
                        id=message.get("message_id") or self._new_hex("message"),
                        thread_id=thread_id,
                        sender=message.get("from", "unknown@example.com"),
                        to=message.get("to", self.account_email),
                        subject=message.get("subject") or thread.get("subject") or "No Subject",
                        body=message.get("body", ""),
                        timestamp=now - parse_age(message.get("age", "1d")),
                        labels=list(message.get("labels", ["INBOX"])),
                        cc=list(message.get("cc", [])),
                        attachments=list(message.get("attachments", [])),
                    )
                    self.messages[msg.id] = msg

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> "FakeGmail":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), **kwargs)

    def set_latency(self, seconds: float, action: Optional[str] = None) -> None:
        with self._lock:
            if action is None:
                self._default_latency = float(seconds)
            else:
                self._latency[action] = float(seconds)

    def fail_next(
        self,
        action: str,
        *,
        exception: Optional[Exception] = None,
        unsuccessful: bool = False,
        after_effect: bool = False,
        times: int = 1,
    ) -> None:
        """Inject a failure: raise ``exception`` (optionally after the effect happened) or return successful=False."""
        with self._lock:
            self._failures.setdefault(action, []).append(
                _Failure(exception=exception, unsuccessful=unsuccessful, after_effect=after_effect, remaining=times)
            )

    def inject_email(
        self,
        *,
        sender: str,
        subject: str,
        body: str,
        to: Optional[str] = None,
        thread_id: Optional[str] = None,
        message_id: Optional[str] = None,
        labels: Iterable[str] = ("INBOX",),
        age: Any = None,
    ) -> FakeMessage:
        with self._lock:
            msg = FakeMessage(
                id=message_id or self._new_hex("message"),
                thread_id=thread_id or self._new_hex("thread"),
                sender=sender,
                to=to or self.account_email,
                subject=subject,
                body=body,
                timestamp=self._now() - parse_age(age),
                labels=list(labels),
            )
            self.messages[msg.id] = msg
            return msg

    # --------------------------------------------------------------- queries
    def actions_log(self) -> List[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._log)

    def external_sends(self) -> List[Dict[str, Any]]:
        """Actions that actually sent an email (including ones whose response was lost)."""
        return [entry for entry in self.actions_log() if entry["action"] in EXTERNAL_SEND_ACTIONS and entry["effect"]]

    def thread_messages(self, thread_id: str) -> List[FakeMessage]:
        with self._lock:
            return sorted((m for m in self.messages.values() if m.thread_id == thread_id), key=lambda m: m.timestamp)

    # ------------------------------------------------------------- execution
    def execute(self, tool_name: str, user_id: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute one action like ``client.tools.execute`` (blocking in sync mode)."""
        args = dict(arguments or {})
        latency = self._latency.get(tool_name, self._default_latency)
        if self.mode == "sync" and latency > 0:
            time.sleep(latency)  # deliberately blocking, like the Composio SDK

        if tool_name not in SUPPORTED_ACTIONS:
            raise RuntimeError(f"FakeGmail does not implement {tool_name}")

        with self._lock:
            failure = self._take_failure(tool_name)
            if failure is not None and not failure.after_effect:
                self._record(tool_name, args, effect=False, ok=False, outcome="injected_failure")
                if failure.exception is not None:
                    raise failure.exception
                return self._envelope({}, successful=False, error="Injected failure")

            handler = getattr(self, "_do_" + tool_name[len("GMAIL_"):].lower())
            data, error, effect, details = handler(args)
            successful = error is None
            self._record(tool_name, args, effect=effect, ok=successful, outcome="ok" if successful else "error", **details)

            if failure is not None:  # after_effect: the action happened, then the response was lost
                self._log[-1]["outcome"] = "injected_failure_after_effect"
                if failure.exception is not None:
                    raise failure.exception
                return self._envelope({}, successful=False, error="Injected failure after effect")
            return self._envelope(data, successful=successful, error=error)

    # ---------------------------------------------------------------- helpers
    def _take_failure(self, action: str) -> Optional[_Failure]:
        queue = self._failures.get(action)
        if not queue:
            return None
        failure = queue[0]
        failure.remaining -= 1
        if failure.remaining <= 0:
            queue.pop(0)
        return failure

    def _envelope(self, data: Dict[str, Any], *, successful: bool, error: Optional[str]) -> Dict[str, Any]:
        self._counter += 1
        return {
            "data": data,
            "error": error,
            "log_id": f"fake-log-{self._counter}",
            "session_info": None,
            "successful": successful,
        }

    def _digest(self, kind: str) -> str:
        self._counter += 1
        return hashlib.sha256(f"{self._seed}:{kind}:{self._counter}".encode()).hexdigest()

    def _new_hex(self, kind: str) -> str:
        return self._digest(kind)[:16]

    def _new_draft_id(self) -> str:
        return "r-" + str(10**18 + int(self._digest("draft")[:15], 16) % (9 * 10**18))

    def _record(self, action: str, args: Dict[str, Any], *, effect: bool, ok: bool, outcome: str, **details: Any) -> None:
        recipients = sorted(
            set(
                emails_in(args.get("recipient_email"))
                + emails_in(args.get("cc"))
                + emails_in(args.get("bcc"))
                + emails_in(args.get("extra_recipients"))
                + emails_in(details.pop("recipients", None))
            )
        )
        detail_body = details.pop("body", None)
        body = args.get("message_body") or args.get("body") or detail_body or args.get("additional_text") or ""
        entry = {
            **_context_fields(),
            "seq": len(self._log) + 1,
            "action": action,
            "at": _iso(self._now()),
            "effect": effect,
            "ok": ok,
            "outcome": outcome,
            "recipients": recipients,
            "body_sha256": hashlib.sha256(normalize_body(body).encode()).hexdigest() if body else None,
            "args": {k: v for k, v in args.items() if k not in ("body", "message_body", "additional_text")},
        }
        entry.update(details)
        self._log.append(entry)

    def _message_payload(self, msg: FakeMessage, include_payload: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "messageId": msg.id,
            "threadId": msg.thread_id,
            # Microseconds: the watcher compares arrival times against the previous poll.
            "messageTimestamp": msg.timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "subject": msg.subject,
            "sender": msg.sender,
            "to": msg.to,
            "labelIds": list(msg.labels),
            "messageText": msg.body,
            "preview": {"subject": msg.subject, "body": msg.body[:100]},
            "attachmentList": list(msg.attachments),
        }
        if include_payload:
            payload["payload"] = {"mimeType": "text/plain", "body": {"data": _b64(msg.body)}}
        return payload

    def search(self, query: str, *, include_spam_trash: bool = False) -> QueryResult:
        parsed = parse_query(query or "", self._now())
        if parsed.unsupported:
            self.unsupported_query_operators += len(parsed.unsupported)
            _emit("fake_gmail_query", unsupported=len(parsed.unsupported))

        results = [
            m
            for m in self.messages.values()
            if (include_spam_trash or not ({"SPAM", "TRASH"} & set(m.labels)))
            and "DRAFT" not in m.labels
            and parsed.matches(m)
        ]
        results.sort(key=lambda m: (m.timestamp, m.id), reverse=True)
        return QueryResult(results, parsed.unsupported)

    # ------------------------------------------------------------- handlers
    # Each returns (data, error, effect, details-for-log).
    def _do_fetch_emails(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str], bool, Dict[str, Any]]:
        max_results = int(args.get("max_results") or 10)
        result = self.search(args.get("query") or "", include_spam_trash=bool(args.get("include_spam_trash")))
        include_payload = bool(args.get("include_payload", True))
        messages = [self._message_payload(m, include_payload) for m in result.messages[:max_results]]
        data = {"messages": messages, "nextPageToken": None, "resultSizeEstimate": len(result.messages)}
        details = {
            "query": args.get("query"),
            "returned": len(messages),
            "returned_threads": sorted({m["threadId"] for m in messages}),
            "unsupported": result.unsupported,
        }
        return data, None, False, details

    def _do_create_email_draft(self, args: Dict[str, Any]):
        recipient = args.get("recipient_email")
        if not recipient:
            return {}, "recipient_email is required", False, {}
        thread_id = args.get("thread_id")
        if thread_id and not any(m.thread_id == thread_id for m in self.messages.values()):
            return {}, f"Thread {thread_id} not found", False, {}
        msg = FakeMessage(
            id=self._new_hex("message"),
            thread_id=thread_id or self._new_hex("thread"),
            sender=self.account_email,
            to=", ".join([recipient, *list(args.get("extra_recipients") or [])]),
            subject=args.get("subject") or "",
            body=args.get("body") or "",
            timestamp=self._now(),
            labels=["DRAFT"],
            cc=list(args.get("cc") or []),
        )
        draft_id = self._new_draft_id()
        self.messages[msg.id] = msg
        self.drafts[draft_id] = msg.id
        data = {"id": draft_id, "message": {"id": msg.id, "threadId": msg.thread_id, "labelIds": ["DRAFT"]}}
        return data, None, True, {"draft_id": draft_id, "thread_id": msg.thread_id, "message_id": msg.id}

    def _do_send_draft(self, args: Dict[str, Any]):
        draft_id = args.get("draft_id")
        message_id = self.drafts.pop(draft_id, None) if draft_id else None
        if message_id is None:
            return {}, f"Draft {draft_id} not found", False, {"draft_id": draft_id}
        msg = self.messages[message_id]
        msg.labels = ["SENT"]
        msg.timestamp = self._now()
        data = {"id": msg.id, "threadId": msg.thread_id, "labelIds": ["SENT"]}
        details = {
            "draft_id": draft_id,
            "thread_id": msg.thread_id,
            "message_id": msg.id,
            "recipients": [msg.to, *msg.cc],
            "body": msg.body,
        }
        return data, None, True, details

    def _do_reply_to_thread(self, args: Dict[str, Any]):
        thread_id = args.get("thread_id")
        thread = [m for m in self.messages.values() if m.thread_id == thread_id and "DRAFT" not in m.labels]
        if not thread_id or not thread:
            return {}, f"Thread {thread_id} not found", False, {"thread_id": thread_id}
        if not args.get("recipient_email"):
            return {}, "recipient_email is required", False, {"thread_id": thread_id}
        subject = sorted(thread, key=lambda m: m.timestamp)[0].subject
        msg = FakeMessage(
            id=self._new_hex("message"),
            thread_id=thread_id,
            sender=self.account_email,
            to=", ".join([args["recipient_email"], *list(args.get("extra_recipients") or [])]),
            subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
            body=args.get("message_body") or "",
            timestamp=self._now(),
            labels=["SENT"],
            cc=list(args.get("cc") or []),
        )
        self.messages[msg.id] = msg
        data = {"id": msg.id, "threadId": thread_id, "labelIds": ["SENT"]}
        return data, None, True, {"thread_id": thread_id, "message_id": msg.id}

    def _do_forward_message(self, args: Dict[str, Any]):
        original = self.messages.get(args.get("message_id") or "")
        if original is None:
            return {}, f"Message {args.get('message_id')} not found", False, {"message_id": args.get("message_id")}
        if not args.get("recipient_email"):
            return {}, "recipient_email is required", False, {}
        msg = FakeMessage(
            id=self._new_hex("message"),
            thread_id=original.thread_id,
            sender=self.account_email,
            to=args["recipient_email"],
            subject=f"Fwd: {original.subject}",
            body=((args.get("additional_text") or "") + "\n\n---------- Forwarded message ---------\n" + original.body).strip(),
            timestamp=self._now(),
            labels=["SENT"],
        )
        self.messages[msg.id] = msg
        data = {"id": msg.id, "threadId": msg.thread_id, "labelIds": ["SENT"]}
        return data, None, True, {"forwarded_message_id": original.id, "thread_id": msg.thread_id, "message_id": msg.id}

    def _do_delete_draft(self, args: Dict[str, Any]):
        draft_id = args.get("draft_id")
        message_id = self.drafts.pop(draft_id, None) if draft_id else None
        if message_id is None:
            return {}, f"Draft {draft_id} not found", False, {"draft_id": draft_id}
        self.messages.pop(message_id, None)
        return {}, None, True, {"draft_id": draft_id}

    def _do_list_drafts(self, args: Dict[str, Any]):
        drafts = []
        for draft_id, message_id in list(self.drafts.items())[: int(args.get("max_results") or 100)]:
            msg = self.messages[message_id]
            entry: Dict[str, Any] = {"id": draft_id, "message": {"id": msg.id, "threadId": msg.thread_id}}
            if args.get("verbose"):
                entry["message"].update({"subject": msg.subject, "to": msg.to, "messageText": msg.body})
            drafts.append(entry)
        return {"drafts": drafts, "resultSizeEstimate": len(drafts)}, None, False, {}

    def _person(self, contact: Dict[str, str], index: int) -> Dict[str, Any]:
        return {
            "resourceName": f"people/c{index}",
            "names": [{"displayName": contact.get("name", "")}],
            "emailAddresses": [{"value": contact.get("email", "")}],
        }

    def _do_get_contacts(self, args: Dict[str, Any]):
        people = [self._person(c, i) for i, c in enumerate(self.contacts, start=1)]
        return {"connections": people, "totalPeople": len(people)}, None, False, {}

    def _do_get_people(self, args: Dict[str, Any]):
        resource = args.get("resource_name") or "people/me"
        if resource == "people/me" and not args.get("other_contacts"):
            return {"resourceName": "people/me", "emailAddresses": [{"value": self.account_email}]}, None, False, {}
        people = [self._person(c, i) for i, c in enumerate(self.contacts, start=1)]
        return {"connections": people}, None, False, {}

    def _do_search_people(self, args: Dict[str, Any]):
        query = (args.get("query") or "").lower()
        matches = [
            {"person": self._person(c, i)}
            for i, c in enumerate(self.contacts, start=1)
            if query and (query in c.get("name", "").lower() or query in c.get("email", "").lower())
        ]
        return {"results": matches}, None, False, {}

    def _do_get_profile(self, args: Dict[str, Any]):
        threads = {m.thread_id for m in self.messages.values()}
        data = {"emailAddress": self.account_email, "messagesTotal": len(self.messages), "threadsTotal": len(threads)}
        return data, None, False, {}



# ---------------------------------------------------------------------------
# Gmail query parser (the subset the fixture can answer)
# ---------------------------------------------------------------------------
#
# Grammar, following Gmail's documented behaviour:
#   query   := and_expr
#   and_expr:= or_expr+                implicit AND (an explicit uppercase AND is a no-op)
#   or_expr := unary ("OR" unary)*     OR binds tighter than the implicit AND:
#                                      "a OR b c" == "(a OR b) AND c"
#   unary   := "-" unary | primary     negation
#   primary := "(" and_expr ")" | "{" unary* "}" (OR of its items) | term
#   term    := operator ":" (word | "phrase" | "(" group ")") | "phrase" | word
# Operators: from: to: cc: bcc: subject: label: in: category: newer_than: older_than:
# after: before: has:attachment thread:. Anything else (is:unread, larger:, ...) is
# recorded as unsupported and ignored (matches everything), like the old matcher.
# Malformed input (unbalanced brackets) never raises: it is recorded and parsed best effort.
# Words match case-insensitive substrings of subject, body, sender and recipients.

Predicate = Callable[[FakeMessage], bool]
MaybePredicate = Optional[Predicate]  # None = an ignored (unsupported) term, dropped from AND and OR
_TOKEN_SPLIT_RE = re.compile(r'"[^"]*"?|[(){}]|[^\s(){}"]+(?:"[^"]*"?)?')
_DATE_RE = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$")
_RELATIVE_RE = re.compile(r"^(\d+)([dhmy])$", re.IGNORECASE)
_RELATIVE_UNITS = {"d": timedelta(days=1), "h": timedelta(hours=1), "m": timedelta(days=30), "y": timedelta(days=365)}


class ParsedQuery:
    def __init__(self, predicate: MaybePredicate, unsupported: List[str]) -> None:
        self._predicate = predicate
        self.unsupported = unsupported

    def matches(self, message: FakeMessage) -> bool:
        return True if self._predicate is None else self._predicate(message)


def _all(preds: List[MaybePredicate]) -> MaybePredicate:
    kept = [p for p in preds if p is not None]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else (lambda m: all(p(m) for p in kept))


def _any(preds: List[MaybePredicate]) -> MaybePredicate:
    kept = [p for p in preds if p is not None]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else (lambda m: any(p(m) for p in kept))


def _not(pred: MaybePredicate) -> MaybePredicate:
    return None if pred is None else (lambda m: not pred(m))


def _parse_date(value: str) -> Optional[datetime]:
    match = _DATE_RE.match(value)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day, tzinfo=UTC)  # the fake interprets dates in UTC
        except ValueError:
            return None
    if value.isdigit():  # epoch seconds, also accepted by Gmail
        return datetime.fromtimestamp(int(value), tz=UTC)
    return None


class _QueryParser:
    def __init__(self, query: str, now: datetime) -> None:
        self.tokens = _TOKEN_SPLIT_RE.findall(query)
        self.pos = 0
        self.now = now
        self.unsupported: List[str] = []

    def peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> Optional[str]:
        token = self.peek()
        self.pos += 1
        return token

    def parse(self) -> ParsedQuery:
        predicate = self.and_expr(stop=None)
        while self.peek() is not None:  # stray closing brackets
            self.unsupported.append(f"unbalanced '{self.take()}'")
            predicate = _all([predicate, self.and_expr(stop=None)])
        return ParsedQuery(predicate, self.unsupported)

    def and_expr(self, stop: Optional[str]) -> MaybePredicate:
        preds: List[MaybePredicate] = []
        while True:
            token = self.peek()
            if token is None or token == stop or token in (")", "}"):
                break
            if token == "AND":
                self.take()
                continue
            preds.append(self.or_expr(stop))
        return _all(preds)

    def or_expr(self, stop: Optional[str]) -> MaybePredicate:
        preds = [self.unary(stop)]
        while self.peek() == "OR":
            self.take()
            if self.peek() in (None, stop, ")", "}"):
                self.unsupported.append("dangling OR")
                break
            preds.append(self.unary(stop))
        return preds[0] if len(preds) == 1 else _any(preds)

    def unary(self, stop: Optional[str]) -> MaybePredicate:
        token = self.peek()
        if token == "-":  # "-(a b)" or "- term"
            self.take()
            if self.peek() in (None, stop, ")", "}"):
                self.unsupported.append("dangling '-'")
                return None
            return _not(self.unary(stop))
        if token is not None and token.startswith("-") and len(token) > 1:
            self.tokens[self.pos] = token[1:]
            return _not(self.unary(stop))
        return self.primary(stop)

    def primary(self, stop: Optional[str]) -> MaybePredicate:
        token = self.take()
        if token == "(":
            inner = self.and_expr(stop=")")
            if self.take() != ")":
                self.unsupported.append("unbalanced '('")
            return inner
        if token == "{":
            items: List[MaybePredicate] = []
            while self.peek() not in (None, "}"):
                if self.peek() in ("OR", "AND"):
                    self.take()
                    continue
                items.append(self.unary(stop="}"))
            if self.take() != "}":
                self.unsupported.append("unbalanced '{'")
            return _any(items)
        return self.term(token or "")

    def term(self, token: str) -> MaybePredicate:
        if token.startswith('"'):
            phrase = token.strip('"').lower()
            return (lambda m: phrase in m.searchable()) if phrase else None
        if ":" in token and not token.startswith(":"):
            op, _, value = token.partition(":")
            if not value and self.peek() == "(":  # operator applied to a group: subject:(a b)
                self.take()
                words = []
                while self.peek() not in (None, ")"):
                    words.append(self.take())
                if self.take() != ")":
                    self.unsupported.append("unbalanced '('")
                return _all([self.operator(op.lower(), w.strip('"'), token) for w in words if w not in ("OR", "AND")])
            return self.operator(op.lower(), value.strip('"'), token)
        word = token.lower()
        return (lambda m: word in m.searchable()) if word else None

    def operator(self, op: str, value: str, raw: str) -> MaybePredicate:
        v = value.lower()
        if not v:
            self.unsupported.append(raw)
            return None
        if op == "from":
            return lambda m: v in m.sender.lower()
        if op == "to":
            return lambda m: v in m.to.lower() or any(v in c.lower() for c in m.cc)
        if op in ("cc", "bcc"):
            return lambda m: any(v in c.lower() for c in m.cc)
        if op == "subject":
            return lambda m: v in m.subject.lower()
        if op in ("label", "in"):
            if v == "anywhere":
                return lambda m: True
            label = v.upper()
            return lambda m: label in m.labels
        if op == "category":
            label = "INBOX" if v == "primary" else f"CATEGORY_{v.upper()}"
            return lambda m: label in m.labels
        if op == "has" and v in ("attachment", "attachments"):
            return lambda m: bool(m.attachments)
        if op == "thread":
            return lambda m: m.thread_id.lower() == v
        if op in ("newer_than", "older_than"):
            match = _RELATIVE_RE.match(v)
            if not match:
                self.unsupported.append(raw)
                return None
            cutoff = self.now - int(match.group(1)) * _RELATIVE_UNITS[match.group(2).lower()]
            return (lambda m: m.timestamp >= cutoff) if op == "newer_than" else (lambda m: m.timestamp < cutoff)
        if op in ("after", "before"):
            when = _parse_date(v)
            if when is None:
                self.unsupported.append(raw)
                return None
            return (lambda m: m.timestamp >= when) if op == "after" else (lambda m: m.timestamp < when)
        self.unsupported.append(raw)  # e.g. is:unread (no such fixture field), larger:, filename:
        return None


def parse_query(query: str, now: Optional[datetime] = None) -> ParsedQuery:
    """Parse a Gmail search query into a predicate. Never raises."""
    try:
        return _QueryParser(query, now or _default_now()).parse()
    except Exception as exc:  # pragma: no cover - last resort: match everything, record why
        return ParsedQuery(None, [f"parse error: {type(exc).__name__}"])

# ---------------------------------------------------------------------------
# Process-wide instance (used by execute_gmail_tool in fake mode)
# ---------------------------------------------------------------------------

FIXTURE_ENV = "OPENPOKE_FAKE_GMAIL_FIXTURE"
_instance: Optional[FakeGmail] = None
_instance_lock = threading.Lock()


def get_fake_gmail() -> FakeGmail:
    global _instance
    with _instance_lock:
        if _instance is None:
            fixture_path = os.environ.get(FIXTURE_ENV, "").strip()
            _instance = FakeGmail.from_file(fixture_path) if fixture_path else FakeGmail()
        return _instance


def install_fake_gmail(instance: Optional[FakeGmail]) -> Optional[FakeGmail]:
    """Replace the process-wide fake (None = lazily rebuild from the env fixture)."""
    global _instance
    with _instance_lock:
        _instance = instance
    return instance


__all__ = [
    "EXTERNAL_SEND_ACTIONS",
    "FakeGmail",
    "FakeMessage",
    "emails_in",
    "get_fake_gmail",
    "install_fake_gmail",
    "normalize_body",
    "parse_age",
]
