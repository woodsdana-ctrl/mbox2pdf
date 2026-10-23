"""The reviewers' per-attachment decisions (``--decisions``).

The input is the survey's own ``Attachment survey.csv`` with its
``decision`` column filled in, in Excel. A row's ``filename`` (exact, any
case, or a pattern with ``*`` and ``?``) or its ``sha256`` says which
attachments it covers; a row may be added by hand with only a pattern, for a
report whose name changes daily (``DailyReport_*.xlsx``).

Decisions:

``latest``
    The attachments the row covers are copies of one recurring report. Only
    the copy from the most recent message is produced; every other copy is
    withheld: not converted, represented by a one-page note that names it and
    points at the latest copy, and listed in the exceptions table. Rows with
    a filename (or pattern) group by it; a row with only a sha256 groups the
    attachments that share that attachment's filename.
``omit``
    The attachments the row covers are not produced at all: not converted,
    a one-page note in every slot that carried them, a row in the exceptions
    table. The row must carry a note, and the note is what those pages say:
    whether an attachment is out of scope or not public, and on what
    authority, is the reviewers' call, so the tool adds no wording of its own. A row
    with a sha256 omits exactly those bytes (the triage index and the survey
    have one on every row); only a row with a filename or pattern and no
    sha256 omits by name.
``produce`` (or blank)
    The default. On a sha256 row it also exempts those exact bytes from any
    ``latest`` or ``omit`` rule.

Nothing is withheld silently: every withheld slot keeps a page, and the
manifest records every rule and what it matched.
"""

from __future__ import annotations

import csv
import fnmatch
import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DECISIONS = ("latest", "omit", "produce")
WILDCARDS = ("*", "?", "[")


@dataclass(frozen=True)
class Rule:
    line: int  # 1-based line of the row in the file, for messages
    decision: str  # "latest" | "omit" | "produce"
    filename: str  # exact name or pattern, "" when the row has only a sha256
    sha256: str  # "" when the row has only a filename
    note: str = ""  # the reviewers' words: added to a latest note, the whole of an omit note

    @property
    def pattern(self) -> bool:
        return any(ch in self.filename for ch in WILDCARDS)

    def covers_name(self, name: str) -> bool:
        if not self.filename:
            return False
        name = name.casefold()
        base = name.rsplit("/", 1)[-1]  # a file that came out of a zip is "zip/name"
        if self.pattern:
            wanted = self.filename.casefold()
            return fnmatch.fnmatchcase(name, wanted) or fnmatch.fnmatchcase(base, wanted)
        return self.filename.casefold() in (name, base)


def withheld_at(withheld_by: str = "") -> str:
    """How the pages say who withheld a copy: "withheld at the direction of"
    and ``--withheld-by``'s name, or the neutral wording when none is given."""
    return f"withheld at the direction of {withheld_by}" if withheld_by else "withheld at the reviewer's direction"


@dataclass
class Withheld:
    """Why one attachment's bytes are not produced."""

    rule: Rule
    group: str  # the report's name, as the kept copy carries it
    copies: int  # distinct copies in the group, the kept one included
    kept_sha256: str
    kept_seq: int  # the message that shows the kept copy in full
    kept_date: datetime | None  # that message's date
    kept_subject: str = ""  # and its subject

    def reason(self, withheld_by: str = "") -> str:
        text = (
            f"it was {withheld_at(withheld_by)}: it is an earlier copy of the recurring "
            f"report “{self.group}”, and only the latest is included, attached to {self._message()}"
        )
        if self.rule.note:
            text += f". Note: {self.rule.note}"
        return text

    def summary(self) -> str:
        """One line for the exceptions table, which lists every withheld copy:
        the full reason (and the reviewers' note) is on the copy's own note page."""
        return (
            f"Withheld: an earlier copy of a recurring report ({self.copies:,} copies); "
            f"the latest is attached to message {self._where()}"
        )

    def _where(self) -> str:
        """The message showing the kept copy, with that message's own date."""
        d = self.kept_date
        return f"{self.kept_seq} ({d:%B} {d.day}, {d.year})" if d else str(self.kept_seq)

    def _message(self) -> str:
        """The same message named by date and subject for the note page, which
        is released: a message number or a count of copies there would tell
        the requester how much was collected. The exceptions table and the CSV
        keep both."""
        d = self.kept_date
        subject = self.kept_subject or "(no subject)"
        return f"the message of {d:%B} {d.day}, {d.year}, “{subject}”" if d else f"the undated message “{subject}”"

    def outcome(self) -> str:
        """The survey or triage CSV's outcome column."""
        return f"withheld: an earlier copy; the latest of {self.copies:,} is attached to message {self.kept_seq}"


@dataclass
class Omitted:
    """An attachment an ``omit`` row keeps out of the production. The
    reviewers' note is the whole of what the pages say about why."""

    rule: Rule

    def reason(self, withheld_by: str = "") -> str:  # the note says it all; withheld_by is not used
        note = " ".join(self.rule.note.split())
        return note if note[-1:] in ".!?" else note + "."

    def summary(self) -> str:
        return f"Not included: {self.reason()}"

    def outcome(self) -> str:
        return "not included (omit)"


@dataclass
class Outcome:
    """What the rules did, for the run and the manifest."""

    withheld: dict[str, Withheld | Omitted] = field(default_factory=dict)  # sha256 -> why
    matched: dict[int, int] = field(default_factory=dict)  # rule line -> attachment slots covered
    kept: dict[int, list[str]] = field(default_factory=dict)  # rule line -> sha256 kept per group

    def unmatched(self, rules: list[Rule]) -> list[Rule]:
        return [r for r in rules if not self.matched.get(r.line)]

    def results(self) -> dict[str, str]:
        """sha256 -> what a decision did to those bytes, for the outcome
        column of the survey CSV and the triage index."""
        out = {sha: why.outcome() for sha, why in self.withheld.items()}
        for shas in self.kept.values():
            for sha in shas:
                copies = 1 + sum(1 for w in self.withheld.values() if getattr(w, "kept_sha256", None) == sha)
                out[sha] = f"produced: the latest of {copies:,} copies"
        return out


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def load(path: Path | str) -> list[Rule]:
    """Rows with a decision, from a survey CSV saved by Excel (comma-separated
    UTF-8 or cp1252, or tab-separated "Unicode Text"). Raises ValueError, with
    the line, on a decision it does not know or a row that names nothing."""
    text = _decode(Path(path).read_bytes())
    first = text.split("\n", 1)[0]
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    fields = {(name or "").strip().casefold(): name for name in reader.fieldnames or []}
    if "decision" not in fields:
        raise ValueError(f"{path}: no 'decision' column (fill one in on the survey's Attachment survey.csv)")

    def get(row: dict, key: str) -> str:
        name = fields.get(key)
        return (row.get(name) or "").strip() if name else ""

    rules: list[Rule] = []
    for row in reader:
        decision = get(row, "decision").casefold()
        if not decision:
            continue
        line = reader.line_num
        if decision not in DECISIONS:
            raise ValueError(
                f"{path}:{line}: unknown decision {decision!r} (use {', '.join(DECISIONS[:-1])} or {DECISIONS[-1]}, or leave it blank)"
            )
        filename, sha = get(row, "filename"), get(row, "sha256").lower()
        if not filename and not sha:
            raise ValueError(f"{path}:{line}: the row has a decision but no filename or sha256 to apply it to")
        if decision == "omit" and not get(row, "note"):
            raise ValueError(
                f"{path}:{line}: an omit row needs a note: it is printed where the attachment was attached, "
                "and in the exceptions table"
            )
        rules.append(Rule(line, decision, filename, sha, get(row, "note")))
    return rules


def _when(dt: datetime | None) -> float:
    return dt.timestamp() if dt else float("-inf")


def apply(entries, rules: list[Rule]) -> Outcome:
    """Decide which attachment bytes to withhold. *entries* are the organized
    messages in final order (message ``seq`` is position + 1)."""
    outcome = Outcome()
    slots = [(seq, entry.model.date, att) for seq, entry in enumerate(entries, start=1) for att in entry.model.attachments]
    subjects = {seq: entry.model.subject for seq, entry in enumerate(entries, start=1)}
    produce = {r.sha256 for r in rules if r.decision == "produce" and r.sha256}
    for rule in rules:
        if rule.decision == "produce":
            outcome.matched[rule.line] = sum(1 for _, _, a in slots if a.sha256 == rule.sha256 or rule.covers_name(a.filename))
            continue
        if rule.decision == "omit":
            # The sha256 wins: the row for one Budget.xlsx must not also omit
            # a different file that happens to share its name.
            if rule.sha256:
                members = [s for s in slots if s[2].sha256 == rule.sha256]
            else:
                members = [s for s in slots if rule.covers_name(s[2].filename)]
            outcome.matched[rule.line] = len(members)
            for _, _, att in members:
                if att.sha256 not in outcome.withheld and att.sha256 not in produce:
                    outcome.withheld[att.sha256] = Omitted(rule)
            continue
        if rule.filename:
            members = [s for s in slots if rule.covers_name(s[2].filename)]
        else:
            names = {a.filename.casefold() for _, _, a in slots if a.sha256 == rule.sha256}
            members = [s for s in slots if s[2].filename.casefold() in names]
        outcome.matched[rule.line] = len(members)
        members = [s for s in members if s[2].sha256 not in outcome.withheld]  # an earlier rule decided these
        if not members:
            continue
        latest: dict[str, tuple[float, int]] = {}  # sha256 -> (latest carrier date, its seq)
        first_seq: dict[str, int] = {}
        for seq, date, att in members:
            latest[att.sha256] = max(latest.get(att.sha256, (float("-inf"), 0)), (_when(date), seq))
            first_seq.setdefault(att.sha256, seq)
        kept = max(latest, key=lambda sha: latest[sha])
        kept_date = next(date for seq, date, att in members if seq == first_seq[kept])
        kept_name = next(att.filename for _, _, att in members if att.sha256 == kept)
        outcome.kept.setdefault(rule.line, []).append(kept)
        for sha in latest:
            if sha == kept or sha in produce:
                continue
            outcome.withheld[sha] = Withheld(
                rule=rule,
                group=kept_name.rsplit("/", 1)[-1],
                copies=len(latest),
                kept_sha256=kept,
                kept_seq=first_seq[kept],
                kept_date=kept_date,
                kept_subject=subjects[first_seq[kept]],
            )
    return outcome
