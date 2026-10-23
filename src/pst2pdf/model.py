"""Shared data model for the pst2pdf pipeline.

Every stage communicates through these dataclasses. Keep them plain: no
behaviour beyond trivial properties, so they pickle cleanly across the render
worker processes and are easy to build in tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Attachment:
    """A real (non-inline) attachment as carried by a message."""

    filename: str
    content: bytes
    content_type: str

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class DiskAttachment:
    """Run-local attachment spool; bytes are loaded only by the active converter."""

    filename: str
    path: Path
    content_type: str
    size: int
    sha256: str

    @property
    def content(self) -> bytes:
        return self.path.read_bytes()


@dataclass
class Participant:
    """One parsed address from From/To/Cc, for the redaction word list."""

    name: str  # display name, may be empty
    address: str  # lowercase email address, may be empty for name-only entries


@dataclass
class MessageModel:
    """One parsed item (mail message or calendar entry) from the archive."""

    source: Path
    folder: str  # PST folder path relative to the archive root, "/"-separated, e.g. "Inbox/Projects"
    subject: str
    from_addr: str
    to_addrs: str
    cc_addrs: str
    date: datetime | None
    body_html: str  # fragment safe to drop into the template; no <a href>, no alt text
    attachments: list[Attachment] = field(default_factory=list)
    bcc_addrs: str = ""
    original_date: str = ""
    original_timestamp: str | None = None
    source_format: str = "pst"
    source_associations: list[dict] = field(default_factory=list)
    gmail_labels: list[str] = field(default_factory=list)
    intake_issues: list[str] = field(default_factory=list)
    identity_fingerprint: str = ""
    kind: str = "mail"  # "mail" or "calendar"
    message_id: str | None = None  # normalized: stripped of <>, whitespace; None when absent
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    thread_index: str | None = None  # raw Outlook Thread-Index header value
    participants: list[Participant] = field(default_factory=list)
    has_images: bool = False  # inline images present in body_html
    had_rtf_body: bool = False
    content_hash: str = ""  # sha256 over normalized headers+body, dedup fallback when message_id is None


@dataclass
class ConvertedAttachment:
    """Outcome of converting one attachment to PDF pages."""

    attachment: Attachment
    status: str  # "converted" | "passthrough" | "placeholder" | "withheld" (--decisions) | "native" (--triage: written as the original file)
    method: str  # "libreoffice" | "image" | "text" | "html" | "pdf" | "message" | "calendar" | "none"
    pdf_path: Path | None  # content pages; None when status == "placeholder"
    page_count: int = 0
    reason: str = ""  # human-readable, filled when status is "placeholder" (e.g. "password protected") or "withheld"
    summary: str = ""  # a shorter reason for the exceptions table, when the full one is long (withheld)
    decision: str = ""  # with status "withheld": the reviewers' decision behind it, "latest" or "omit"


@dataclass
class AttachmentEntry:
    """One attachment slot behind a message, as it will appear in a volume."""

    index: int  # 1-based position within the message
    total: int
    filename: str
    size: int
    content_type: str
    sha256: str
    converted: ConvertedAttachment | None  # None when duplicate_of is set
    duplicate_of: int | None = None  # seq of the first message that carried identical content
    separator_pdf: Path | None = None  # the page that stands in for an attachment with no pages of its own (placeholder, cross-reference, zip contents, appendix pointer), rendered by report.py
    appendix_item: int | None = None  # with --appendix-over: the appendix item holding this attachment's pages; the slot itself is a one-page pointer
    file_id: str | None = None  # with --triage: the NativeFile this slot carried ("A0042")


@dataclass
class MessageBundle:
    """A message plus everything that will follow it in the volumes."""

    seq: int  # 1-based, in final output order
    model: MessageModel
    folders: list[str]  # every folder this message was found in (>1 only after message dedup)
    thread_id: str
    message_pdf: Path
    message_pages: int = 0
    copies: int = 1  # times the archive held this message (>1 only after message dedup)
    attachments: list[AttachmentEntry] = field(default_factory=list)


@dataclass
class AppendixItem:
    """One attachment moved out of the message stream into the appendix
    volumes (``--appendix-over``). Items are numbered in order of their first
    reference in the message stream. With attachment dedup on, every slot
    carrying the same bytes points at one item; with it off, each slot has its
    own item."""

    number: int  # 1-based
    filename: str
    size: int
    content_type: str
    sha256: str
    converted: ConvertedAttachment  # status "converted" or "passthrough"
    carriers: list[tuple[int, int]] = field(default_factory=list)  # (seq, attachment index) of every slot pointing here
    header_pdf: Path | None = None  # the one-page item header sheet, rendered by report.py


@dataclass
class NativeFile:
    """One distinct attachment in a triage run (``--triage``), written once
    as the original file. IDs follow the first reference in the message
    stream and are assigned before any decision is looked at, so the same
    archive gives the same IDs whatever the decisions file says."""

    id: str  # "A0042"
    filename: str  # as attached; "archive.zip/inner.xlsx" for a file from a zip
    size: int
    content_type: str
    sha256: str
    kind: str  # convert.detect's kind ("office", "pdf", ...), "" when nothing recognized it
    status: str  # "file" (written), "withheld" (an earlier copy, latest) or "omitted" (omit)
    file_name: str | None = None  # name in the folder; None when not written
    carriers: list[tuple[int, int]] = field(default_factory=list)  # (seq, attachment index) of every slot


@dataclass
class PageRange:
    """Inclusive Bates range."""

    start: int
    end: int


@dataclass
class Volume:
    number: int
    total: int
    path: Path
    bates: PageRange
    first_seq: int
    last_seq: int
    page_count: int = 0


@dataclass
class Plan:
    """Where every page lands, computed before any stamping happens."""

    volumes: list[Volume]
    message_ranges: dict[int, PageRange]  # seq -> Bates range for the message pages only
    attachment_ranges: dict[tuple[int, int], PageRange]  # (seq, index) -> range incl. separator
    bundle_ranges: dict[int, PageRange]  # seq -> message + all attachments
    front_matter: PageRange | None  # index pages at the front of volume 1, if any
    # With --appendix-over: the appendix volumes, numbered on from the last
    # message-volume page. Their Volume.first_seq/last_seq hold item numbers.
    appendix_volumes: list[Volume] = field(default_factory=list)
    appendix_ranges: dict[int, PageRange] = field(default_factory=dict)  # item -> header sheet + content


@dataclass
class AttachmentSize:
    """One distinct attachment (by sha256) and the pages it puts in the volumes.

    Built by ``survey.measure`` for START HERE's "Largest attachments", the
    manifest and the ``--survey`` report.
    """

    sha256: str
    filename: str  # as carried by its first copy
    content_type: str
    size: int  # bytes
    status: str  # ConvertedAttachment.status: converted | passthrough | placeholder | container
    method: str  # ConvertedAttachment.method; "none" for a placeholder or container
    reason: str  # why it is a placeholder, "" otherwise
    pages: int  # pages of one converted copy; 0 for a placeholder or container
    copies: int  # attachment slots (after message dedup) that carried these bytes
    full_copies: int  # slots that show the pages in full: 1 when deduped, copies otherwise
    first_seq: int  # the first message that carried it
    first_date: datetime | None

    @property
    def pages_in_volumes(self) -> int:
        """Content pages these bytes put in the volumes. Later copies of a
        deduped attachment are one-page cross-references, counted as notes."""
        return self.pages * self.full_copies


@dataclass
class TypeTotal:
    """Attachments of one conversion method, for the survey's breakdown."""

    method: str  # ConvertedAttachment.method, or "placeholder" / "container"
    label: str  # plain words for the reviewer
    attachments: int  # distinct files
    copies: int  # attachment slots
    pages: int  # content pages in the volumes


@dataclass
class WhatIf:
    """What moving every attachment longer than *threshold* pages out of the
    message volumes would do to them."""

    threshold: int
    attachments: int  # distinct attachments longer than threshold
    pages_moved: int  # content pages leaving the message volumes
    notes_added: int  # one-page notes left in their place
    pages_left: int  # message-volume pages afterwards
    volumes_left: int  # about this many volumes at the run's --volume-pages


@dataclass
class PageBudget:
    """Where the pages of a run go. ``survey.measure`` fills it from the
    bundles and the plan; START HERE, the manifest and ``--survey`` read it."""

    messages: int
    message_pages: int
    attachment_slots: int
    distinct_attachments: int  # distinct sha256 across every slot
    attachment_pages: int  # converted pages in the volumes (full copies only)
    note_pages: int  # one-page stand-ins: cross-references, placeholders, zip sheets
    front_matter_pages: int  # index plus exceptions table
    total_pages: int
    volumes: int
    volume_pages: int  # the --volume-pages the plan was made with
    exceptions: int  # attachments that could not be converted plus header-only messages
    attachments: list[AttachmentSize] = field(default_factory=list)  # largest first
    by_type: list[TypeTotal] = field(default_factory=list)  # most pages first
    what_if: list[WhatIf] = field(default_factory=list)  # ascending thresholds; empty with --appendix-over
    appendix_over: int | None = None  # the --appendix-over the plan was made with
    appendix_items: int = 0  # distinct attachments in the appendix volumes
    appendix_pages: int = 0  # appendix volume pages, header sheets included (part of total_pages)
    appendix_volumes: int = 0  # not counted in ``volumes``, which are message volumes


@dataclass
class SearchReport:
    """The Purview search behind the archive (``--search-report``), for START
    HERE and the manifest. Text fields are exactly as Purview wrote them;
    ``source`` is "report" (read from the export's Summary report) or "text"
    (a plain file holding only the query, so every other field is empty)."""

    query: str
    source: str
    sha256: str  # of the file given, or of the Summary CSV inside a zip
    job_id: str = ""
    case_name: str = ""
    search_name: str = ""
    export_name: str = ""
    started: str = ""  # "StartTime (UTC)" as written, such as "1/31/2026 4:05:06 PM"
    finished: str = ""
    security_filter: str = ""  # "Security Filter Applied": "Yes" or "No"
    location_restriction: str = ""
    content_restriction: str = ""


@dataclass
class BatesSpec:
    prefix: str = "DOC-"
    start: int = 1
    width: int = 6

    def label(self, number: int) -> str:
        return f"{self.prefix}{number:0{self.width}d}"


@dataclass
class Options:
    """Everything the CLI decides, passed down instead of loose keyword args."""

    request_id: str
    input_format: str = "pst"
    display_timezone: str | None = None
    export_source: str = "unknown"
    search_query: str = ""
    export_description: str = ""
    order: str = "thread"  # "thread" | "chrono" | "folder"
    bates: BatesSpec = field(default_factory=BatesSpec)
    stamp_bates: bool = True  # print the Bates label on every page; off also drops every page reference from the index, exceptions table and cross-references
    volume_pages: int = 1000
    dedup_messages: bool = True
    dedup_attachments: bool = True
    convert_attachments: bool = True
    keep_natives: bool = False
    expand_zips: bool = True  # zip attachments' files become attachments too
    terms_file: Path | None = None
    decisions_file: Path | None = None  # --decisions: the survey CSV with the reviewers' decision column filled in
    withheld_by: str = ""  # --withheld-by: who a "latest" note says withheld a copy; "" = neutral wording
    search_report: SearchReport | None = None  # --search-report: the Purview query, on START HERE and in the manifest
    patterns: list[str] = field(default_factory=list)  # e.g. ["phone", "student-id"]
    student_id_regex: str | None = None
    overlay: dict[str, str] | None = None  # premark category -> overlay text (e.g. a statute citation)
    survey: bool = False  # count pages and write the attachment survey instead of volumes
    triage: bool = False  # attachments as original files plus an index for the reviewers' decisions; nothing converted
    jobs: int | None = None
    attachment_timeout: int = 120  # seconds per LibreOffice conversion
    attachment_max_bytes: int = 100 * 1024 * 1024
    attachment_max_pages: int = 2000  # a converted attachment longer than this becomes a placeholder
    appendix_over: int | None = None  # move converted attachments longer than this many pages to appendix volumes; None = off
