"""Identifier extraction (single owner; used by 2B journaling, the registry, the card and the resolver).

The execution-agent journal truncates tool arguments to 200 chars and tool
results to 500 (mechanism M2), which drops exactly what follow-ups need: the
draft id to send, the thread id to reply to. These functions pull those
identifiers out of the raw tool call, structurally and deterministically.

Rules (documented in docs/overload/02_DESIGN.md):
- walk arguments and results recursively; keys are normalized (lowercase, ``_`` removed);
- ``threadid`` -> thread, ``messageid`` -> message, ``draftid`` -> draft;
- ``id`` inside a ``draft``/``drafts`` object -> draft; inside ``message``/``messages``
  -> message; an ``id`` that looks like a Gmail draft id (``r-<digits>``) -> draft;
  an ``id`` next to a ``thread_id`` (an email object, e.g. task_email_search results) -> message;
- recipient-like keys (``recipient_email``, ``to``, ``cc``, ``bcc``,
  ``extra_recipients``, ``sender``, ``recipient``, ``from``) and search queries -> emails;
- at most ``IDS_PER_KIND_CAP`` values per kind; unknown shapes yield empty sets;
  extraction NEVER raises.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set

from ...overload_config import IDS_PER_KIND_CAP

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
GMAIL_HEX_RE = re.compile(r"\b[0-9a-f]{16}\b")
DRAFT_ID_RE = re.compile(r"\br-?\d{10,}\b")

_THREAD_KEYS = {"threadid"}
_MESSAGE_KEYS = {"messageid", "forwardedmessageid"}
_DRAFT_KEYS = {"draftid"}
_EMAIL_KEYS = {"recipientemail", "to", "cc", "bcc", "extrarecipients", "sender", "recipient", "from", "emailaddress"}
_QUERY_KEYS = {"query", "searchquery"}
_DRAFT_PARENTS = {"draft", "drafts"}
_MESSAGE_PARENTS = {"message", "messages", "emails"}


def _norm_key(key: Any) -> str:
    return str(key).lower().replace("_", "")


@dataclass(frozen=True)
class ExtractedIds:
    threads: FrozenSet[str] = field(default_factory=frozenset)
    messages: FrozenSet[str] = field(default_factory=frozenset)
    drafts: FrozenSet[str] = field(default_factory=frozenset)
    emails: FrozenSet[str] = field(default_factory=frozenset)
    gmail_ids: FrozenSet[str] = field(default_factory=frozenset)  # from free text: thread OR message

    def is_empty(self) -> bool:
        return not (self.threads or self.messages or self.drafts or self.emails or self.gmail_ids)

    def merge(self, other: "ExtractedIds") -> "ExtractedIds":
        return ExtractedIds(
            threads=self.threads | other.threads,
            messages=self.messages | other.messages,
            drafts=self.drafts | other.drafts,
            emails=self.emails | other.emails,
            gmail_ids=self.gmail_ids | other.gmail_ids,
        )

    def strong(self) -> Set[str]:
        """Identity-level values (everything except emails)."""
        return set(self.threads | self.messages | self.drafts | self.gmail_ids)

    def to_journal(self, tool: str) -> Dict[str, Any]:
        return {
            "tool": tool,
            "threads": sorted(self.threads),
            "messages": sorted(self.messages),
            "drafts": sorted(self.drafts),
            "emails": sorted(self.emails),
        }

    @classmethod
    def from_journal(cls, payload: Any) -> "ExtractedIds":
        try:
            data = json.loads(payload) if isinstance(payload, str) else dict(payload)
            return cls(
                threads=frozenset(data.get("threads", [])),
                messages=frozenset(data.get("messages", [])),
                drafts=frozenset(data.get("drafts", [])),
                emails=frozenset(e.lower() for e in data.get("emails", [])),
            )
        except Exception:
            return EMPTY


EMPTY = ExtractedIds()


class _Collector:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.kinds: Dict[str, List[str]] = {"threads": [], "messages": [], "drafts": [], "emails": [], "gmail_ids": []}

    def add(self, kind: str, value: Any) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value:
            return
        bucket = self.kinds[kind]
        if kind == "emails":
            for email in EMAIL_RE.findall(value):
                email = email.lower()
                if email not in bucket and len(bucket) < self.cap:
                    bucket.append(email)
            return
        if value not in bucket and len(bucket) < self.cap:
            bucket.append(value)

    def add_many(self, kind: str, value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                self.add_many(kind, item)
        else:
            self.add(kind, value)

    def result(self) -> ExtractedIds:
        return ExtractedIds(**{kind: frozenset(values) for kind, values in self.kinds.items()})


def _walk(obj: Any, parent: str, out: _Collector, depth: int = 0) -> None:
    if depth > 12:  # pathological nesting: stop quietly
        return
    if isinstance(obj, dict):
        keys = {_norm_key(k) for k in obj}
        looks_like_email = "threadid" in keys
        for key, value in obj.items():
            nk = _norm_key(key)
            if nk in _THREAD_KEYS:
                out.add("threads", value)
            elif nk in _MESSAGE_KEYS:
                out.add("messages", value)
            elif nk in _DRAFT_KEYS:
                out.add("drafts", value)
            elif nk == "id" and isinstance(value, str):
                if parent in _DRAFT_PARENTS or DRAFT_ID_RE.fullmatch(value.strip()):
                    out.add("drafts", value)
                elif parent in _MESSAGE_PARENTS or looks_like_email:
                    out.add("messages", value)
            elif nk in _EMAIL_KEYS:
                out.add_many("emails", value)
            elif nk in _QUERY_KEYS:
                out.add_many("emails", value)
            if isinstance(value, (dict, list, tuple)):
                _walk(value, nk, out, depth + 1)
            elif isinstance(value, str) and value[:1] in "[{" and nk in ("result", "data", "content"):
                _walk(_maybe_json(value), nk, out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk(item, parent, out, depth + 1)


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def extract_from_tool_call(tool: str, args: Any, result: Any, *, cap: Optional[int] = None) -> ExtractedIds:
    """Identifiers in one tool call. Errors: extract from args only. Never raises."""
    try:
        out = _Collector(int(cap if cap is not None else IDS_PER_KIND_CAP.get()))
        _walk(_maybe_json(args) or {}, "", out)
        result = _maybe_json(result)
        is_error = isinstance(result, dict) and set(result) <= {"error"} and "error" in result
        if not is_error:
            _walk(result, "", out)
        return out.result()
    except Exception:
        return EMPTY


def extract_from_text(text: Any, *, cap: Optional[int] = None) -> ExtractedIds:
    """Emails, 16-hex Gmail ids (kind ``gmail_id``: thread or message) and Gmail draft ids in free text."""
    try:
        if not isinstance(text, str) or not text:
            return EMPTY
        out = _Collector(int(cap if cap is not None else IDS_PER_KIND_CAP.get()))
        out.add("emails", text)
        for match in GMAIL_HEX_RE.findall(text):
            out.add("gmail_ids", match)
        for match in DRAFT_ID_RE.findall(text):
            out.add("drafts", match)
        return out.result()
    except Exception:
        return EMPTY


REF_SUBJECT_CHARS = 80  # enough to recognise an email; refs are an index, not content


def email_refs(result: Any, *, cap: Optional[int] = None) -> List[Dict[str, str]]:
    """Compact id->email map for list-of-email results (task_email_search, fetch).

    Bare id sets cannot tell a follow-up WHICH thread is "the Harbor Law email";
    one ref per email (message id, thread id, sender, short subject) can.
    Never raises.
    """
    try:
        limit = int(cap if cap is not None else IDS_PER_KIND_CAP.get())
        result = _maybe_json(result)
        if isinstance(result, dict):
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            result = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(result, list):
            return []
        refs: List[Dict[str, str]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            message_id = item.get("id") or item.get("messageId") or item.get("message_id")
            thread_id = item.get("thread_id") or item.get("threadId")
            if not (isinstance(message_id, str) and isinstance(thread_id, str)):
                continue
            refs.append({
                "message": message_id,
                "thread": thread_id,
                "from": str(item.get("sender") or "")[:REF_SUBJECT_CHARS],
                "subject": str(item.get("subject") or "")[:REF_SUBJECT_CHARS],
            })
            if len(refs) >= limit:
                break
        return refs
    except Exception:
        return []


def merge_all(items: Iterable[ExtractedIds]) -> ExtractedIds:
    merged = EMPTY
    for item in items:
        if item is not None:
            merged = merged.merge(item)
    return merged


__all__ = ["DRAFT_ID_RE", "EMAIL_RE", "EMPTY", "ExtractedIds", "email_refs", "extract_from_text", "extract_from_tool_call", "merge_all"]
