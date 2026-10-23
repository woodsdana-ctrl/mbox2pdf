"""Dedup, thread, and order parsed messages for the review volumes.

Everything here is pure: it takes the parsed ``MessageModel`` list and the
run ``Options`` and returns the final sequence. Nothing touches disk.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .model import MessageModel, Options

log = logging.getLogger(__name__)

THREAD_INDEX_PREFIX = 22  # base64 chars of Thread-Index that identify the root

# Reply/forward prefixes ("Re:", "FW:", "Re[2]:", "AW:") and short bracketed
# tags such as an organization's "[EXTERNAL]" banner, in any order and any
# number, so "Re: [EXTERNAL] Re: X" and "[EXTERNAL] Re: X" are one thread.
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"\[[^\]]{1,20}\]\s*"
    r"|(?:re|fw|fwd|aw|wg|sv|vs|tr|antw)\s*(?:\[\d+\]|\(\d+\))?\s*:\s*"
    r")+",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]*>")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass
class Organized:
    """One message as it will appear in the output, in final order."""

    model: MessageModel
    folders: list[str] = field(default_factory=list)
    thread_id: str = ""
    copies: int = 1  # how many times the archive held this message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _date_key(dt: datetime | None) -> tuple[int, datetime]:
    """Sortable key: dated first (UTC, naive), undated last.

    ``parsedate_to_datetime`` yields aware datetimes except for ``-0000``,
    which is naive; calendar all-day items are naive too. Comparing the two
    raises, so everything is flattened to naive UTC here, treating naive
    values as already UTC.
    """
    if dt is None:
        return (1, datetime.max)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (0, dt)


def _source_key(model: MessageModel) -> str:
    return str(model.source)


def body_fingerprint(body_html: str) -> str:
    """Loose hash of a body: tags stripped, URLs removed, whitespace collapsed.

    Purview exports the same message once per mailbox searched, and the
    copies differ only in the Safe Links rewriting of every URL, so URLs are
    ignored. Anything else that differs (a sentence, a name, a number) still
    counts, so a sender who reuses someone else's Message-ID cannot make a
    different message disappear as a "duplicate".
    """
    text = _TAG_RE.sub(" ", body_html or "")
    text = _URL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dedup_key(model: MessageModel) -> str:
    """Message-ID plus the loose body fingerprint; content hash without an ID."""
    if model.source_format == "mbox":
        return f"mbox|{model.message_id or ''}|{model.identity_fingerprint or model.content_hash}"
    if model.message_id:
        return f"{model.message_id}|{body_fingerprint(model.body_html)}"
    return model.content_hash or _source_key(model)


def normalize_subject(subject: str) -> str:
    """Strip reply/forward prefixes and whitespace, lowercase."""
    s = _SUBJECT_PREFIX_RE.sub("", subject or "")
    s = _WS_RE.sub(" ", s).strip().lower()
    return s


def thread_index_prefix(thread_index: str | None) -> str | None:
    if not thread_index:
        return None
    compact = _WS_RE.sub("", thread_index)
    if len(compact) < THREAD_INDEX_PREFIX:
        return None
    return compact[:THREAD_INDEX_PREFIX]


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Keep the lower index as root for determinism.
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def deduplicate(models: list[MessageModel], enabled: bool = True) -> list[Organized]:
    """Collapse duplicates by Message-ID and body fingerprint (content hash
    fallback when there is no ID).

    Keeps the first copy in sorted-source order and records every folder the
    message was found in. Two messages that share a Message-ID but carry
    different bodies are both kept, with a warning naming them.
    """
    ordered = sorted(models, key=_source_key)
    if not enabled:
        return [Organized(model=m, folders=[m.folder]) for m in ordered]

    by_key: dict[str, Organized] = {}
    by_id: dict[str, Organized] = {}
    out: list[Organized] = []
    for model in ordered:
        key = dedup_key(model)
        entry = by_key.get(key)
        if entry is None:
            entry = Organized(model=model, folders=[model.folder])
            by_key[key] = entry
            out.append(entry)
            if model.message_id:
                first = by_id.setdefault(model.message_id, entry)
                if first is not entry:
                    log.warning(
                        "Message-ID <%s> is shared by %s and %s but their content differs; keeping both",
                        model.message_id, first.model.source, model.source,
                    )
        else:
            entry.model.source_associations.extend(model.source_associations)
            entry.model.gmail_labels = list(dict.fromkeys(entry.model.gmail_labels + model.gmail_labels))
            entry.model.intake_issues = list(dict.fromkeys(entry.model.intake_issues + model.intake_issues))
            entry.copies += 1
            if model.folder not in entry.folders:
                entry.folders.append(model.folder)
    return out


def _thread_groups(entries: list[Organized]) -> list[list[int]]:
    """Return groups of entry indexes that belong to the same thread."""
    uf = _UnionFind(len(entries))

    # Conflicting MBOX IDs are ambiguous; do not silently join their versions.
    ambiguous = {e.model.message_id for e in entries if e.model.source_format == "mbox"
                 and any("Conflicting Message-ID" in issue for issue in e.model.intake_issues)}
    # 1. Message-ID / In-Reply-To / References.
    id_owner: dict[str, int] = {}
    for i, entry in enumerate(entries):
        m = entry.model
        ids = [m.message_id, m.in_reply_to, *m.references]
        for mid in ids:
            if not mid or mid in ambiguous:
                continue
            owner = id_owner.setdefault(mid, i)
            if owner != i:
                uf.union(owner, i)

    # 2. Outlook Thread-Index root prefix.
    prefix_owner: dict[str, int] = {}
    for i, entry in enumerate(entries):
        prefix = thread_index_prefix(entry.model.thread_index) if entry.model.source_format != "mbox" else None
        if prefix is None:
            continue
        owner = prefix_owner.setdefault(prefix, i)
        if owner != i:
            uf.union(owner, i)

    # 3. Same normalized subject AND at least one shared participant address.
    by_subject: dict[str, list[int]] = {}
    for i, entry in enumerate(entries):
        subj = normalize_subject(entry.model.subject) if entry.model.source_format != "mbox" else ""
        if subj:
            by_subject.setdefault(subj, []).append(i)
    for members in by_subject.values():
        if len(members) < 2:
            continue
        addr_owner: dict[str, int] = {}
        for i in members:
            for p in entries[i].model.participants:
                if not p.address:
                    continue
                owner = addr_owner.setdefault(p.address, i)
                if owner != i:
                    uf.union(owner, i)

    groups: dict[int, list[int]] = {}
    for i in range(len(entries)):
        groups.setdefault(uf.find(i), []).append(i)
    return list(groups.values())


def _message_sort_key(entry: Organized) -> tuple:
    return (*_date_key(entry.model.date), _source_key(entry.model))


def assign_threads(entries: list[Organized]) -> None:
    """Fill ``thread_id`` on every entry in place."""
    for group in _thread_groups(entries):
        members = sorted((entries[i] for i in group), key=_message_sort_key)
        earliest = members[0].model
        seed = earliest.message_id or earliest.content_hash or _source_key(earliest)
        if earliest.source_format == "mbox":
            seed += "|" + (earliest.identity_fingerprint or _source_key(earliest))
        thread_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        for entry in members:
            entry.thread_id = thread_id


def order(entries: list[Organized], how: str) -> list[Organized]:
    if how == "chrono":
        return sorted(entries, key=_message_sort_key)
    if how == "folder":
        return sorted(entries, key=lambda e: (e.model.folder, *_message_sort_key(e)))
    if how == "thread":
        by_thread: dict[str, list[Organized]] = {}
        for entry in entries:
            by_thread.setdefault(entry.thread_id, []).append(entry)
        threads = []
        for members in by_thread.values():
            members.sort(key=_message_sort_key)
            threads.append((_message_sort_key(members[0]), members))
        threads.sort(key=lambda t: t[0])
        return [entry for _, members in threads for entry in members]
    raise ValueError(f"unknown order {how!r}; expected thread, chrono, or folder")


def organize(models: list[MessageModel], options: Options) -> list[Organized]:
    """Dedup (optional), thread, and order the parsed messages."""
    versions: dict[str, set[str]] = {}
    for m in models:
        if m.source_format == "mbox" and m.message_id:
            versions.setdefault(m.message_id, set()).add(dedup_key(m))
    for m in models:
        if m.source_format == "mbox" and m.message_id and len(versions[m.message_id]) > 1:
            m.intake_issues.append("Conflicting Message-ID; distinct content retained")
    entries = deduplicate(models, enabled=options.dedup_messages)
    assign_threads(entries)
    return order(entries, options.order)
