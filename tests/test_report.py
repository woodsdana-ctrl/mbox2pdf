"""Report pages, tested as HTML so they run without WeasyPrint.

The sample builders at the top are shared with test_render.py and with the
Docker render check; every name in them is invented.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import pytest

from pst2pdf import report
from pst2pdf.model import (
    Attachment,
    AttachmentEntry,
    BatesSpec,
    ConvertedAttachment,
    MessageBundle,
    MessageModel,
    Options,
    PageRange,
    Participant,
    Plan,
    Volume,
)

TZ = timezone(timedelta(hours=-5))
BASE_DATE = datetime(2026, 3, 2, 9, 15, tzinfo=TZ)

# Body text chosen to exercise ligature suppression: "office", "file", "affirm".
BODY = (
    "<p>Please see the attached office budget file and affirm the figures by Friday. "
    "The difficult part is the staffing line.</p>"
    "<p>Best,<br>Alice</p>"
)


def make_model(
    seq: int,
    subject: str = "Quarterly budget",
    folder: str = "Inbox",
    kind: str = "mail",
    has_images: bool = False,
    attachments: list[Attachment] | None = None,
    to_addrs: str = "Bob Example <bob@example.com>",
    cc_addrs: str = "",
) -> MessageModel:
    return MessageModel(
        source=Path(f"/work/{folder}/{seq}.eml"),
        folder=folder,
        subject=subject,
        from_addr="Alice Example <alice@example.com>",
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        date=BASE_DATE + timedelta(days=seq, minutes=7 * seq),
        body_html=BODY,
        attachments=attachments or [],
        kind=kind,
        message_id=f"<m{seq}@example.com>",
        participants=[
            Participant("Alice Example", "alice@example.com"),
            Participant("Bob Example", "bob@example.com"),
        ],
        has_images=has_images,
    )


def make_entry(
    index: int,
    total: int,
    filename: str,
    status: str = "converted",
    method: str = "libreoffice",
    page_count: int = 3,
    reason: str = "",
    duplicate_of: int | None = None,
    size: int = 84 * 1024,
    content_type: str = "application/octet-stream",
) -> AttachmentEntry:
    att = Attachment(filename=filename, content=b"x" * 16, content_type=content_type)
    converted = None
    if duplicate_of is None:
        converted = ConvertedAttachment(
            attachment=att,
            status=status,
            method=method,
            pdf_path=None if status == "placeholder" else Path(f"/work/att/{filename}.pdf"),
            page_count=0 if status == "placeholder" else page_count,
            reason=reason,
        )
    return AttachmentEntry(
        index=index,
        total=total,
        filename=filename,
        size=size,
        content_type=content_type,
        sha256="0" * 64,
        converted=converted,
        duplicate_of=duplicate_of,
    )


def sample_options(**overrides) -> Options:
    kwargs = dict(request_id="2026-0001", bates=BatesSpec(prefix="DOC-", start=1, width=6))
    kwargs.update(overrides)
    return Options(**kwargs)


def sample_bundles() -> list[MessageBundle]:
    """Four messages: one with three attachments (converted, placeholder, and
    one duplicated later), a calendar item, a deduped message found in two
    folders with a long recipient list, and one carrying the duplicate
    attachment plus pictures."""
    b1 = MessageBundle(
        seq=1,
        model=make_model(
            1,
            attachments=[
                Attachment("Budget FY27.xlsx", b"x", "application/vnd.ms-excel"),
                Attachment("scan.zip", b"x", "application/zip"),
                Attachment("Board memo.docx", b"x", "application/msword"),
            ],
        ),
        folders=["Inbox"],
        thread_id="abc123",
        message_pdf=Path("/work/msg/1.pdf"),
        message_pages=2,
        attachments=[
            make_entry(1, 3, "Budget FY27.xlsx", page_count=4),
            make_entry(2, 3, "scan.zip", status="placeholder", method="none", reason="zip archives are not opened"),
            make_entry(3, 3, "Board memo.docx", page_count=2),
        ],
    )
    b2 = MessageBundle(
        seq=2,
        model=make_model(2, subject="Budget review meeting", kind="calendar", folder="Calendar"),
        folders=["Calendar"],
        thread_id="abc123",
        message_pdf=Path("/work/msg/2.pdf"),
        message_pages=1,
    )
    b3 = MessageBundle(
        seq=3,
        model=make_model(
            3,
            subject="Re: Quarterly budget",
            folder="Inbox/Projects",
            to_addrs=(
                "Bob Example <bob@example.com>, Carol Example <carol@example.com>, "
                "Dan Example <dan@example.com>, Eve Example <eve@example.com>, "
                "Frank Example <frank@example.com>, Grace Example <grace@example.com>"
            ),
            cc_addrs='"Example, Heidi" <heidi@example.com>',
        ),
        folders=["Inbox/Projects", "Sent Items"],
        thread_id="abc123",
        message_pdf=Path("/work/msg/3.pdf"),
        message_pages=3,
    )
    b4 = MessageBundle(
        seq=4,
        model=make_model(4, subject="Fwd: Quarterly budget", has_images=True),
        folders=["Inbox"],
        thread_id="abc123",
        message_pdf=Path("/work/msg/4.pdf"),
        message_pages=1,
        attachments=[make_entry(1, 1, "Budget FY27.xlsx", duplicate_of=1)],
    )
    return [b1, b2, b3, b4]


def sample_plan(bundles: list[MessageBundle], front_matter_pages: int = 2, split: bool = False) -> Plan:
    """Lay the bundles out the way assemble.plan would, so ranges are consistent."""
    next_page = 1
    front = None
    if front_matter_pages:
        front = PageRange(1, front_matter_pages)
        next_page = front_matter_pages + 1
    message_ranges: dict[int, PageRange] = {}
    attachment_ranges: dict[tuple[int, int], PageRange] = {}
    bundle_ranges: dict[int, PageRange] = {}
    for b in bundles:
        start = next_page
        message_ranges[b.seq] = PageRange(next_page, next_page + b.message_pages - 1)
        next_page += b.message_pages
        for e in b.attachments:
            pages = 1 + (e.converted.page_count if e.converted else 0)
            attachment_ranges[(b.seq, e.index)] = PageRange(next_page, next_page + pages - 1)
            next_page += pages
        bundle_ranges[b.seq] = PageRange(start, next_page - 1)
    last = next_page - 1
    seqs = [b.seq for b in bundles]
    if not split:
        volumes = [Volume(1, 1, Path("/out/Volume 01 of 01.pdf"), PageRange(1, last), seqs[0], seqs[-1], last)]
    else:
        half = len(seqs) // 2
        cut = bundle_ranges[seqs[half]].start
        volumes = [
            Volume(1, 2, Path("/out/Volume 01 of 02.pdf"), PageRange(1, cut - 1), seqs[0], seqs[half - 1], cut - 1),
            Volume(2, 2, Path("/out/Volume 02 of 02.pdf"), PageRange(cut, last), seqs[half], seqs[-1], last - cut + 1),
        ]
    return Plan(
        volumes=volumes,
        message_ranges=message_ranges,
        attachment_ranges=attachment_ranges,
        bundle_ranges=bundle_ranges,
        front_matter=front,
    )


def sample_stats(with_coverage: bool = False) -> dict:
    stats = {
        "messages_found": 5,
        "messages": 4,
        "duplicates_removed": 1,
        "calendar_items": 1,
        "messages_with_images": 1,
        "attachments": 4,
        "attachments_converted": 2,
        "attachments_crossref": 1,
        "attachments_exceptions": 1,
        "started": datetime(2026, 9, 17, 10, 0, tzinfo=TZ),
        "finished": datetime(2026, 9, 17, 10, 1, tzinfo=TZ),
    }
    if with_coverage:
        stats["coverage"] = {
            "term_hits": {"[student] Jane Doe": 12, "[parent] Robert Doe": 0},
            "pattern_hits": {"phone": 3},
            "zero_hits": ["[parent] Robert Doe"],
        }
    return stats


@pytest.fixture
def world():
    bundles = sample_bundles()
    return bundles, sample_plan(bundles), sample_options()


def _text(html: str) -> str:
    """Visible text, roughly: style and tags stripped, entities decoded, whitespace collapsed."""
    html = re.sub(r"<style>.*?</style>", " ", html, flags=re.DOTALL)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


# --- wording helpers --------------------------------------------------------


def test_describe_type_and_method(world):
    bundles, _, _ = world
    xlsx, zipped, docx = bundles[0].attachments
    assert report.describe_type("Budget FY27.xlsx") == "Excel spreadsheet"
    assert report.describe_type("photo.JPG") == "JPEG image"
    assert report.describe_type("thing.bin", "image/x-foo") == "Image"
    assert report.describe_type("thing", "application/octet-stream") == "File"
    assert report.describe_method(xlsx) == "Converted from Excel"
    assert report.describe_method(docx) == "Converted from Word"
    assert report.describe_method(zipped) == "Not converted"
    assert report.describe_method(bundles[3].attachments[0]) == "Identical to an earlier attachment"
    pdf = make_entry(1, 1, "scan.pdf", method="pdf")
    assert report.describe_method(pdf).startswith("Original PDF pages")
    img = make_entry(1, 1, "photo.png", method="image")
    assert report.describe_method(img) == "Converted from an image"
    withheld = make_entry(1, 1, "daily.xlsx", status="withheld", method="none", page_count=0)
    assert report.describe_method(withheld) == "Withheld at the reviewer's direction"
    assert report.describe_method(withheld, "the records custodian") == "Withheld at the direction of the records custodian"


# --- attachment sheets ------------------------------------------------------


def test_placeholder_sheet(world):
    bundles, _, opts = world
    b1 = bundles[0]
    text = _text(report.build_placeholder_html(b1.attachments[1], b1, opts))
    assert "Attachment 2 of 3" in text
    assert "scan.zip" in text
    assert "could not be converted to pages because zip archives are not opened." in text
    assert "listed in the exceptions table behind the index" in text
    assert "was not included in this delivery" in text
    assert "Pages None" in text


def test_placeholder_sheet_mentions_natives_folder_when_kept(world):
    bundles, _, _ = world
    b1 = bundles[0]
    text = _text(report.build_placeholder_html(b1.attachments[1], b1, sample_options(keep_natives=True)))
    assert report.NATIVES_DIR_NAME in text
    assert "has not been redacted" in text


def test_placeholder_does_not_claim_an_exceptions_row_when_conversion_was_off(world):
    """With --no-convert-attachments no attachment is an exception, so the
    table does not list it and the page must not say it does."""
    bundles, _, _ = world
    b1 = bundles[0]
    text = _text(report.build_placeholder_html(b1.attachments[1], b1, sample_options(convert_attachments=False)))
    assert "exceptions table" not in text
    assert "was not included in this delivery" in text


def test_placeholder_without_reason_gets_generic_wording(world):
    bundles, _, opts = world
    entry = make_entry(1, 1, "mystery.xyz", status="placeholder", method="none", reason="")
    text = _text(report.build_placeholder_html(entry, bundles[0], opts))
    assert "because it is in a format the conversion tool does not handle." in text


def test_crossref_sheet(world):
    bundles, plan, opts = world
    b1, _, _, b4 = bundles
    first_range = plan.attachment_ranges[(1, 1)]
    text = _text(report.build_crossref_html(b4.attachments[0], b4, b1, first_range, opts, plan))
    assert "Attachment 1 of 1" in text
    assert "Identical to the attachment at DOC-000005 to DOC-000009 in Volume 01 of 01.pdf." in text
    assert "The same file, byte for byte, was attached to the message of Tue, 03 Mar 2026 09:22" in text
    assert "“Quarterly budget”" in text
    assert "Redactions made on those pages cover this copy." in text
    assert "See DOC-000005 to DOC-000009 in Volume 01 of 01.pdf" in text
    # A released page names messages by date and subject, never by number.
    assert "Attached to the message" in text
    assert not re.search(r"[Mm]essage \d", text)
    # Without a plan the volume is simply left out.
    text = _text(report.build_crossref_html(b4.attachments[0], b4, b1, first_range, opts))
    assert "Identical to the attachment at DOC-000005 to DOC-000009." in text


def test_volume_for_bates_number(world):
    bundles, _, _ = world
    plan = sample_plan(bundles, split=True)
    assert report.volume_name(plan, 1) == "Volume 01 of 02.pdf"
    assert report.volume_name(plan, plan.volumes[1].bates.start) == "Volume 02 of 02.pdf"
    assert report.volume_for(plan, plan.volumes[1].bates.end + 1) is None
    assert report.volume_name(None, 1) == ""
    assert report.volume_name(plan, None) == ""


def test_container_sheet(world):
    bundles, _, opts = world
    b1 = bundles[0]
    zipped = b1.attachments[1]
    zipped.converted.status = "container"
    zipped.converted.reason = "secret.docx: password protected\nhuge.bin: larger than the conversion limit"
    b1.attachments = b1.attachments[:2] + [
        make_entry(3, 4, "scan.zip/page1.png", method="image", page_count=1),
        make_entry(4, 4, "scan.zip/notes.txt", method="text", page_count=1),
    ]
    assert [e.index for e in report.container_children(zipped, b1)] == [3, 4]
    text = _text(report.build_container_html(zipped, b1, opts))
    assert "This attachment is a zip archive." in text
    assert "Its 2 files follow as attachments 3 to 4 of this message." in text
    assert "Not shown from inside the archive: secret.docx: password protected huge.bin: larger" in text
    assert "Pages None of its own. Its files follow as attachments 3 to 4." in text
    assert report.describe_method(zipped).startswith("Zip archive")


def test_range_text_collapses_single_page():
    spec = BatesSpec()
    assert report._range_text(spec, PageRange(7, 7)) == "DOC-000007"
    assert report._range_text(spec, PageRange(7, 9)) == "DOC-000007 to DOC-000009"
    assert report._range_text(spec, None) == ""


# --- index ------------------------------------------------------------------


def test_index_rows_and_layout(world):
    bundles, plan, opts = world
    html = report.build_index_html(bundles, plan, opts)
    text = _text(html)
    assert "size: Letter landscape" in html
    assert "<thead>" in html and "table-header-group" in html
    assert "break-inside: avoid" in html
    assert "counter(pages)" in html
    assert "2026-0001 · 4 messages" in text
    # Layout: 2 index pages, then message 1 (2 pages + 5 + 1 + 3 attachment
    # pages), the calendar item (1), message 3 (3), message 4 (1 + 1 cross-ref).
    assert "4 messages in 1 volume, pages DOC-000001 to DOC-000019" in text
    assert "Volume 01 of 01 DOC-000001 to DOC-000019 1 to 4" in text
    # Message 1: two body pages then 3 attachments (5 + 1 + 3 pages) -> 3..13.
    assert "DOC-000003 to 000013" in text
    # Calendar rows are labelled; deduped folders are all listed.
    assert "Calendar: Budget review meeting" in text
    assert "Inbox/Projects, Sent Items" in text
    # Long recipient lists are trimmed to two names, no addresses in the index.
    assert "Bob Example, Carol Example and 4 more" in text
    assert "bob@example.com" not in text
    assert "Example, Heidi" not in text  # Cc is not an index column
    # A bare address in From/To may wrap rather than overflow into Subject.
    assert "td.people { overflow-wrap: anywhere; }" in html
    # Picture flag counts inline images; attachment count includes the cross-ref.
    rows = report._index_rows(bundles, plan, opts)
    assert [r["images"] for r in rows] == [False, False, False, True]
    assert [r["attachments"] for r in rows] == [3, 0, 0, 1]
    assert rows[3]["bates_start"] == "DOC-000018" and rows[3]["bates_end"] == "DOC-000019"
    # The range is printed on one line with the prefix once.
    assert rows[3]["range"] == "DOC-000018 to 000019"
    assert "DOC-000003 to 000013" in text
    assert "marks the 1 message that contains pictures" in text
    # Folders differ, so the Folder column is present.
    assert "<th>Folder</th>" in html
    assert "All messages come from the folder" not in text


def test_index_drops_folder_column_when_all_the_same(world):
    bundles, plan, opts = world
    for b in bundles:
        b.folders = ["Inbox"]
    assert report.common_folder(bundles) == "Inbox"
    html = report.build_index_html(bundles, plan, opts)
    text = _text(html)
    assert "<th>Folder</th>" not in html
    assert "All messages come from the folder Inbox" in text
    bundles[0].folders = []
    bundles[0].model.folder = ""
    assert report.common_folder(bundles) is None


def test_index_prepared_uses_the_given_clock(world):
    bundles, plan, opts = world
    text = _text(report.build_index_html(bundles, plan, opts, prepared=datetime(2001, 2, 3, tzinfo=TZ)))
    assert "prepared February 3, 2001" in text


def test_index_picture_flag_counts_image_attachments(world):
    bundles, plan, opts = world
    bundles[1].attachments = [make_entry(1, 1, "photo.png", method="image", page_count=1)]
    bundles[1].attachments[0].total = 1
    rows = report._index_rows(bundles, plan, opts)
    assert rows[1]["images"] is True


def test_index_with_two_volumes(world):
    bundles, _, opts = world
    plan = sample_plan(bundles, split=True)
    text = _text(report.build_index_html(bundles, plan, opts))
    assert "in 2 volumes" in text
    assert "Volume 01 of 02" in text and "Volume 02 of 02" in text


def test_index_single_page_message_shows_one_label(world):
    bundles, plan, opts = world
    rows = report._index_rows(bundles, plan, opts)
    calendar = rows[1]
    assert calendar["bates_start"] == "DOC-000014"
    assert calendar["bates_end"] == ""


# --- START HERE -------------------------------------------------------------


def test_start_here_contents(world):
    bundles, plan, opts = world
    html = report.build_start_here_html(opts, sample_stats(), plan)
    text = _text(html)
    assert "Start here" in text
    assert "for 2026-0001, prepared September 17, 2026" in text
    assert "1 volume (Volume 01 of 01.pdf)" in text
    assert "such as DOC-000001" in text
    assert "Volume 01 of 01.pdf opens with an index of every message, followed by a list of the 1 item that could not be shown as pages" in text
    assert "Redaction word list.txt : the names and email addresses of everyone who sent, received, or was copied on a message" in text
    assert "manifest.json : a technical record of this run. Keep this file back." in text
    assert "Exceptions.pdf" not in text
    assert ("What you may release: only the redacted, sanitized copies of the volumes. Never release the word list, "
            "the manifest, or an unsanitized volume.") in text
    # Plain sentences, no semicolons.
    assert "Messages: 4 in the volumes, of which 1 is a calendar item. The archive held 5. 1 was an extra copy of the same message (exported from more than one mailbox or folder) and is shown once." in text
    assert "Attachments: 4. 2 were converted to pages. 1 was identical to an earlier one and is replaced by a note pointing to it. 1 could not be converted and is listed behind the index in Volume 01 of 01.pdf." in text
    assert "Pictures: 1 message contains pictures" in text
    assert "Pages: 19, numbered DOC-000001 to DOC-000019." in text
    assert "Order: grouped by conversation" in text
    assert "Word list: covers the names of everyone who sent, received, or was copied on a message." in text
    assert "Calendar items show no organizer or attendees when the export did not include them." in text
    assert ";" not in re.sub(r"&[a-z]+;", "", re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL))
    # The Acrobat steps, in order, matching the 2025-2026 interface.
    steps = ["Open a volume.", "Mark the names.", "Page through.", "Apply and save."]
    positions = [text.index(s) for s in steps]
    assert positions == sorted(positions)
    assert "In All tools choose Redact, then Redact text & images, then Find text." in text
    assert "Pick Multiple words or phrases, click Select words, then Import, and choose Redaction word list.txt from this folder." in text
    assert "Remove the people this request is about and any department or shared-mailbox names, then Search, Check all, and Mark checked results for redaction." in text
    assert "Choose “All PDF documents in” this folder to cover every volume in one pass." in text
    assert "Drag a rectangle over words inside a picture with Redact text & images." in text
    assert "Click Apply, leave “Sanitize and remove hidden information” ticked, and save the copy Acrobat proposes." in text
    assert "split at bookmarks before applying, because sanitizing removes bookmarks. Send only the sanitized copies." in text
    assert "Search & Redact" not in text and "Apply Redactions" not in text
    # No coverage section without premark, and nothing about a run's optional extras.
    assert "Redactions marked in advance" not in text
    assert "DO NOT RELEASE" not in text
    assert "Original attachments" not in text
    assert "not fetched" not in text and "unformatted" not in text


def test_start_here_optional_sentences(world):
    _, plan, opts = world
    stats = sample_stats()
    stats.update(rtf_fallbacks=3, blocked_urls=9, render_fallbacks=2, render_failures=[{"message": 2, "reason": "x"}],
                 skipped_non_mail=2, calendar_items=0, attachments_containers=1)
    text = _text(report.build_start_here_html(opts, stats, plan))
    assert "3 messages arrived in a format whose layout could not be kept. Their text is shown unformatted." in text
    assert "Pictures hosted on the web were not fetched and appear blank." in text
    assert "2 messages could not be laid out as sent and are shown as plain text, or as a header alone when even that failed." in text
    assert "past the edge of the page" not in text  # no off-page fallbacks in this run
    assert "2 items were not messages (contacts or similar) and are not included." in text
    assert "1 was a zip archive whose files are counted here as attachments of their own." in text
    assert "Calendar items show no organizer" not in text
    assert "followed by a list of the 2 items that could not be shown as pages" in text


def test_start_here_when_attachments_not_converted(world):
    _, plan, _ = world
    opts = sample_options(convert_attachments=False)
    text = _text(report.build_start_here_html(opts, sample_stats(), plan))
    assert "Attachments: 4. Attachments were not converted to pages for this run." in text
    assert "could not be converted" not in text
    assert "followed by a list" not in text


def test_start_here_coverage_shows_categories_never_terms(world):
    _, plan, opts = world
    html = report.build_start_here_html(opts, sample_stats(with_coverage=True), plan)
    text = _text(html)
    assert "Redactions marked in advance" in text
    assert "already contain 15 redaction marks" in text
    assert "student 1 12" in text
    assert "parent 1 0" in text
    assert "Pattern: phone 3" in text
    assert "1 of the 2 terms was not found anywhere. Pre-marked terms - DO NOT RELEASE.txt says which." in text
    assert "Jane Doe" not in html and "Robert Doe" not in html
    assert "Pre-marked terms - DO NOT RELEASE.txt : the names and terms that were marked for redaction in advance" in text
    assert "Never release the word list, the manifest, the file marked DO NOT RELEASE, or an unsanitized volume." in text


def test_start_here_accepts_runstats_and_objects(world):
    _, plan, opts = world
    stats = report.RunStats(messages=4, attachments=0)
    text = _text(report.build_start_here_html(opts, stats, plan))
    assert "Messages: 4 in the volumes." in text
    assert "Attachments: 0. Pictures" in text  # no breakdown when there are none

    class Cov:
        term_hits = {"Jane Doe": 2}
        pattern_hits = {}
        zero_hits = []

    stats.coverage = Cov()
    html = report.build_start_here_html(opts, stats, plan)
    text = _text(html)
    assert "other terms 1 2" in text
    assert "Jane Doe" not in html
    assert "not found anywhere" not in text


def test_summarize_coverage_uses_term_categories():
    class Term:
        def __init__(self, text, category):
            self.text, self.category, self.is_pattern = text, category, category == "pattern"

        def phrases(self):
            return [self.text, self.text.split()[-1]]

    class Cov:
        term_hits = {"Jane Doe": 12, "Robert Doe": 0, "Plain": 1}
        phrase_hits = {"Jane Doe": 10, "Doe": 2, "Robert Doe": 0, "Plain": 1}
        pattern_hits = {"phone": 3}

    terms = [Term("Jane Doe", "student"), Term("Robert Doe", "parent"), Term("Plain", None), Term("phone", "pattern")]
    summary = report.summarize_coverage(Cov(), terms)
    assert summary == {
        "categories": [
            {"category": "student", "terms": 1, "marks": 12, "terms_without_hits": 0},
            {"category": "parent", "terms": 1, "marks": 0, "terms_without_hits": 1},
            {"category": "other terms", "terms": 1, "marks": 1, "terms_without_hits": 0},
        ],
        "patterns": [{"pattern": "phone", "marks": 3}],
        "terms": 3,
        "terms_without_hits": 1,
        "total": 16,
    }
    assert report.summarize_coverage(None) is None
    assert report.summarize_coverage({"term_hits": {}, "pattern_hits": {}}) is None


def test_premark_detail_file_lists_every_term(tmp_path):
    class Term:
        def __init__(self, text, category, phrases):
            self.text, self.category, self._phrases = text, category, phrases

        def phrases(self):
            return self._phrases

    class Cov:
        term_hits = {"Jane Doe": 12, "Robert Doe": 0}
        phrase_hits = {"Jane Doe": 10, "Doe, Jane": 2, "Robert Doe": 0}
        pattern_hits = {"phone": 3}

    terms = [Term("Jane Doe", "student", ["Jane Doe", "Doe, Jane"]), Term("Robert Doe", "parent", ["Robert Doe"])]
    out = report.write_premark_detail(tmp_path, Cov(), terms)
    assert out == tmp_path / "Pre-marked terms - DO NOT RELEASE.txt"
    body = out.read_bytes().decode("utf-8")
    assert body.startswith("PRE-MARKED TERMS. DO NOT RELEASE.")
    assert "[student] Jane Doe: 12 marks\r\n    also searched: Doe, Jane (2)\r\n" in body
    assert "[parent] Robert Doe: 0 marks (no matches)\r\n" in body
    assert "[pattern] phone: 3 marks\r\n" in body
    assert report.write_premark_detail(tmp_path, None) is None


def test_start_here_natives_and_orders(world):
    _, plan, _ = world
    text = _text(report.build_start_here_html(sample_options(keep_natives=True, order="chrono"), {}, plan))
    assert "Original attachments - DO NOT RELEASE : the attachments as original files" in text
    assert "by date, oldest first" in text
    text = _text(report.build_start_here_html(sample_options(order="folder"), {}, plan))
    assert "by mailbox folder" in text


def test_start_here_without_plan(world):
    _, _, opts = world
    text = _text(report.build_start_here_html(opts, {}, None))
    assert "0 volumes" in text
    assert "Pages: 0," in text


# --- exceptions -------------------------------------------------------------


def test_exceptions_page(world):
    bundles, plan, opts = world
    rows = report.list_exceptions(bundles, plan, opts)
    assert len(rows) == 1
    assert rows[0].seq == 1 and rows[0].index == 2
    assert rows[0].bates == "DOC-000010"
    assert rows[0].volume == "Volume 01 of 01.pdf"
    html = report.build_exceptions_html(bundles, plan, opts, prepared=datetime(2026, 9, 17, tzinfo=TZ))
    text = _text(html)
    assert "size: Letter landscape" in html
    assert "1 attachment could not be converted to pages · prepared September 17, 2026" in text
    # No Subject column: the message number and page identify the message.
    assert "1 DOC-000010 Volume 01 of 01.pdf 2026-03-03 09:22 scan.zip 84.0 KB Zip archive Zip archives are not opened" in text
    assert "Quarterly budget" not in text
    assert "were not included in this delivery" in text


def test_exceptions_reason_is_first_line_only(world):
    bundles, plan, opts = world
    bundles[0].attachments[1].converted.reason = "password protected\nsecond line with detail"
    rows = report.list_exceptions(bundles, plan, opts)
    assert rows[0].reason == "password protected"


def test_exceptions_skip_containers_and_conversion_off(world):
    bundles, plan, opts = world
    bundles[0].attachments[1].converted.status = "container"
    assert report.list_exceptions(bundles, plan, opts) == []
    bundles[0].attachments[1].converted.status = "placeholder"
    assert report.list_exceptions(bundles, plan, sample_options(convert_attachments=False)) == []


def test_exceptions_list_messages_whose_body_failed(world):
    bundles, plan, opts = world
    failures = [{"message": 3, "reason": "the message body could not be rendered"}]
    rows = report.list_exceptions(bundles, plan, opts, failures)
    assert [(r.seq, r.index) for r in rows] == [(1, 2), (3, 0)]
    assert rows[1].is_message and rows[1].bates == "DOC-000015" and rows[1].volume == "Volume 01 of 01.pdf"
    text = _text(report.build_exceptions_html(bundles, plan, opts, failures))
    assert "1 attachment could not be converted to pages · 1 message could not be laid out" in text
    assert "3 DOC-000015 Volume 01 of 01.pdf 2026-03-05 09:36 (the message itself) Message The message body could not be rendered" in text


def test_exceptions_none_when_clean(world, tmp_path):
    bundles, plan, opts = world
    bundles[0].attachments.pop(1)
    assert report.build_exceptions_html(bundles, plan, opts) is None
    assert report.render_exceptions(bundles, plan, opts, tmp_path / "x.pdf") is None
    assert not (tmp_path / "x.pdf").exists()


# --- manifest ---------------------------------------------------------------


# --- --no-bates: unnumbered pages -------------------------------------------


def test_index_without_bates_has_no_page_references(world):
    bundles, plan, _ = world
    opts = sample_options(stamp_bates=False)
    html = report.build_index_html(bundles, plan, opts)
    text = _text(html)
    assert "DOC-" not in html
    assert 'class="bates"' not in html  # the per-message Pages column is gone
    # The volume table says how long each volume is instead of where it starts.
    assert "Volume 01 of 01 19 1 to 4" in text
    assert "4 messages in 1 volume, 19 pages · prepared" in text
    # The legend points at the message number instead of the page number.
    assert "# is the message number, shown in Acrobat’s bookmarks." in text
    assert "printed at the bottom right" not in text
    # The fixed-layout table still names one width per column it renders.
    assert html.count("<col ") == text_columns(html)


def text_columns(html: str) -> int:
    """Header cells in the per-message index table."""
    body = html.split('class="grid msgs"', 1)[1]
    return len(re.findall(r"<th[ >]", body.split("</thead>", 1)[0]))


def test_index_with_bates_still_shows_the_page_column(world):
    bundles, plan, opts = world
    html = report.build_index_html(bundles, plan, opts)
    assert 'class="bates"' in html
    assert "DOC-000001" in html
    assert html.count("<col ") == text_columns(html)


def test_exceptions_without_bates_drops_the_page_column(world):
    bundles, plan, _ = world
    opts = sample_options(stamp_bates=False)
    html = report.build_exceptions_html(bundles, plan, opts, prepared=datetime(2026, 9, 17, tzinfo=TZ))
    text = _text(html)
    assert "DOC-" not in html
    assert "<th>Page</th>" not in html
    assert "1 Volume 01 of 01.pdf 2026-03-03 09:22 scan.zip 84.0 KB Zip archive Zip archives are not opened" in text
    assert "The message number identifies the message" in text


def test_crossref_without_bates_points_at_the_message(world):
    bundles, plan, _ = world
    b1, _, _, b4 = bundles
    opts = sample_options(stamp_bates=False)
    first_range = plan.attachment_ranges[(1, 1)]
    text = _text(report.build_crossref_html(b4.attachments[0], b4, b1, first_range, opts, plan))
    assert "DOC-" not in text
    assert "Identical to the attachment behind an earlier message in Volume 01 of 01.pdf." in text
    assert "Pages See the message of Tue, 03 Mar 2026 09:22" in text and "in Volume 01 of 01.pdf" in text
    assert not re.search(r"[Mm]essage \d", text)
    # An undated first copy is named by its subject alone.
    b1.model.date = None
    text = _text(report.build_crossref_html(b4.attachments[0], b4, b1, first_range, opts, plan))
    assert "was attached to the undated message, “Quarterly budget”." in text
    assert "Pages See the undated message “Quarterly budget” in Volume 01 of 01.pdf" in text


def test_start_here_without_bates_promises_no_page_numbers(world):
    _, plan, _ = world
    opts = sample_options(stamp_bates=False)
    text = _text(report.build_start_here_html(opts, sample_stats(), plan))
    assert "DOC-" not in text
    assert "Each message is a bookmark in Acrobat’s side panel. Volume 01 of 01.pdf opens with an index" in text
    assert "Pages: 19." in text
    assert "lists every message and whether it contains pictures" in text


def test_manifest(world, tmp_path):
    bundles, plan, opts = world
    vol_path = tmp_path / "Volume 01 of 01.pdf"
    vol_path.write_bytes(b"%PDF-1.7 fake")
    volumes = [Volume(1, 1, vol_path, PageRange(1, 19), 1, 4, 19)]
    out = report.write_manifest(tmp_path, opts, sample_stats(with_coverage=True), plan, volumes, bundles=bundles)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert out.name == "manifest.json"
    assert doc["request_id"] == "2026-0001"
    assert doc["counts"]["messages"] == 4
    assert doc["started"].startswith("2026-09-17T10:00")
    assert doc["bates"] == {
        "stamped": True,
        "prefix": "DOC-", "start": 1, "width": 6, "first": "DOC-000001", "last": "DOC-000019", "pages": 19,
    }
    (vol,) = doc["volumes"]
    assert vol["filename"] == "Volume 01 of 01.pdf"
    assert len(vol["sha256"]) == 64
    assert vol["bates_start"] == "DOC-000001" and vol["bates_end"] == "DOC-000019"
    (exc,) = doc["exceptions"]
    assert exc == {
        "message": 1, "attachment": 2, "filename": "scan.zip", "size": 84 * 1024, "sha256": "0" * 64,
        "content_type": "application/octet-stream", "reason": "zip archives are not opened", "decision": None,
        "bates": "DOC-000010", "volume": "Volume 01 of 01.pdf",
    }
    # Coverage is per category plus the terms file's hash: no term text, no path.
    assert doc["coverage"]["categories"] == [
        {"category": "student", "terms": 1, "marks": 12, "terms_without_hits": 0},
        {"category": "parent", "terms": 1, "marks": 0, "terms_without_hits": 1},
    ]
    assert doc["coverage"]["patterns"] == [{"pattern": "phone", "marks": 3}]
    assert doc["coverage"]["total"] == 15 and doc["coverage"]["terms_without_hits"] == 1
    assert "terms_file_sha256" in doc["coverage"]
    raw = out.read_text(encoding="utf-8")
    assert "Jane Doe" not in raw and "Robert Doe" not in raw
    assert doc["options"]["terms_file"] is False
    assert doc["options"]["order"] == "thread"
    assert doc["options"]["bates"]["prefix"] == "DOC-"
    assert "coverage" not in doc["counts"] and "render_failures" not in doc["counts"]
    assert doc["counts"]["render_fallbacks"] == 0


def test_manifest_terms_file_hash_not_path(world, tmp_path):
    bundles, plan, opts = world
    terms = tmp_path / "secret terms.txt"
    terms.write_text("[student] Jane Doe\n", encoding="utf-8")
    opts = sample_options(terms_file=terms)
    stats = sample_stats(with_coverage=True)
    stats["render_failures"] = [{"message": 2, "reason": "the message body could not be rendered"}]
    out = report.write_manifest(tmp_path, opts, stats, plan, [], bundles=bundles)
    raw = out.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert doc["coverage"]["terms_file_sha256"] == report.sha256_file(terms)
    assert doc["options"]["terms_file"] is True
    assert "secret terms" not in raw and "Jane Doe" not in raw
    assert [e["message"] for e in doc["exceptions"]] == [1, 2]
    assert doc["exceptions"][1]["attachment"] is None and doc["exceptions"][1]["bates"] == "DOC-000014"


def test_manifest_uses_stats_exceptions_without_bundles(world, tmp_path):
    _, plan, opts = world
    stats = sample_stats()
    stats["exceptions"] = [{"message": 9, "attachment": 1, "filename": "x.bin", "reason": "too large"}]
    doc = json.loads(report.write_manifest(tmp_path, opts, stats, plan, []).read_text())
    assert doc["exceptions"][0]["message"] == 9
    assert doc["volumes"] == []


# --- appendix volumes (--appendix-over) ----------------------------------------


def appendix_world(**option_overrides):
    """The sample world with --appendix-over 3: the 4-page spreadsheet on
    message 1 and its identical copy on message 4 both point at appendix item
    1. The 2-page memo stays inline, the zip placeholder always does. Planned
    for real, so ranges are what assemble would produce."""
    from pst2pdf import assemble
    from pst2pdf.model import AppendixItem

    bundles = sample_bundles()
    b1, _, _, b4 = bundles
    xlsx = b1.attachments[0]
    xlsx.appendix_item = 1
    b4.attachments[0].converted = xlsx.converted
    b4.attachments[0].duplicate_of = None
    b4.attachments[0].appendix_item = 1
    item = AppendixItem(
        number=1, filename=xlsx.filename, size=xlsx.size, content_type=xlsx.content_type,
        sha256=xlsx.sha256, converted=xlsx.converted, carriers=[(1, 1), (4, 1)],
    )
    opts = sample_options(appendix_over=3, **option_overrides)
    plan = assemble.plan(bundles, opts, front_matter_pages=2, appendix=[item])
    return bundles, plan, opts, item


def test_appendix_world_plan():
    bundles, plan, _, _ = appendix_world()
    assert [v.bates for v in plan.volumes] == [PageRange(1, 14)]
    assert plan.appendix_ranges == {1: PageRange(15, 19)}  # header sheet + 4 pages
    assert [Path(v.path).name for v in plan.appendix_volumes] == ["Attachments 01 of 01.pdf"]


def test_appendix_pointer_page():
    bundles, plan, opts, item = appendix_world()
    b1, _, _, b4 = bundles
    html = report.build_appendix_pointer_html(b4.attachments[0], b4, item, plan, opts)
    text = _text(html)
    assert "This attachment is in a separate volume" in text
    assert "Attachment 1 of 1" in text and "Budget FY27.xlsx" in text
    assert "Its pages are in Attachments 01 of 01.pdf, at DOC-000015 to DOC-000019." in text
    assert "Attachments longer than 3 pages were moved out of the message volumes so the messages stay together." in text
    assert "The same file was also attached to other messages. It appears in the attachment volume once" in text
    assert "Pages 4 pages, in Attachments 01 of 01.pdf, at DOC-000015 to DOC-000019" in text
    # No item number or count of messages: either says how much was collected.
    assert "item" not in text.lower().replace("items", "") and "2 messages" not in text
    assert "Attached to the message" in text
    assert not re.search(r"[Mm]essage \d", text)
    # Not an earlier message: duplicates point at the appendix, never at message 1.
    assert "message 1" not in text
    # A single carrier says nothing about copies.
    item.carriers = [(1, 1)]
    text = _text(report.build_appendix_pointer_html(b1.attachments[0], b1, item, plan, opts))
    assert "The same file" not in text
    assert ";" not in re.sub(r"&[a-z]+;", "", re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL))


def test_appendix_pointer_page_without_bates():
    bundles, plan, opts, item = appendix_world(stamp_bates=False)
    b4 = bundles[3]
    html = report.build_appendix_pointer_html(b4.attachments[0], b4, item, plan, opts)
    text = _text(html)
    assert "DOC-" not in html
    assert "Its pages are in Attachments 01 of 01.pdf, behind a sheet naming this file." in text
    assert "Pages 4 pages, in Attachments 01 of 01.pdf" in text and "item 1" not in text
    # --appendix-over 0 moves everything, so the note does not call it long.
    opts.appendix_over = 0
    text = _text(report.build_appendix_pointer_html(b4.attachments[0], b4, item, plan, opts))
    assert "Attachments were moved out of the message volumes so the messages stay together." in text


def test_appendix_header_sheet():
    bundles, _, opts, item = appendix_world()
    by_seq = {b.seq: b for b in bundles}
    text = _text(report.build_appendix_header_html(item, by_seq, opts))
    assert "Budget FY27.xlsx" in text
    assert "Item 1" not in text  # an item number says how many long attachments there were
    assert "Size 84.0 KB" in text and "Type Excel spreadsheet" in text
    assert "Conversion Converted from Excel" in text
    assert "Pages 4 pages follow this sheet" in text
    assert "Attached to these messages" in text and "2 messages" not in text
    assert "behind every message it was attached to" in text
    assert "Tue, 03 Mar 2026 09:22" in text and "Quarterly budget attachment 1" in text
    assert "Fwd: Quarterly budget" in text
    assert not re.search(r"[Mm]essage \d", text)
    assert "other message" not in text
    assert "DOC-" not in text  # no page numbers: it is rendered before planning
    # Many carriers: the first few, then a count, so the sheet stays one page.
    item.carriers = [(1, 1), (1, 2)] + [(4, 1)] * 1 + [(seq, 1) for seq in range(5, 12)]
    html = report.build_appendix_header_html(item, by_seq, opts)
    text = _text(html)
    assert "Attached to these messages" in text and "9 messages" not in text
    assert "attachments 1 and 2" in text
    assert html.count('<td class="nowrap">&nbsp;&nbsp;attachment') == report.APPENDIX_HEADER_CARRIERS
    assert "It was also attached to other messages, each with a note pointing here." in text
    assert "4 other" not in text


def test_index_lists_appendix_items_and_volumes():
    bundles, plan, opts, _ = appendix_world()
    html = report.build_index_html(bundles, plan, opts)
    text = _text(html)
    assert "4 messages in 1 volume and 1 attachment volume, pages DOC-000001 to DOC-000019" in text
    assert "Attachments 01 of 01 DOC-000015 to DOC-000019 Item 1" in text
    assert "Attachments in the attachment volumes" in text
    assert "1 DOC-000015 to 000019 Attachments 01 of 01 Budget FY27.xlsx 4 1, 4" in text
    appendix_table = html.split("Attachments in the attachment volumes", 1)[1]
    head = appendix_table.split("</thead>", 1)[0]
    assert head.count("<col ") == len(re.findall(r"<th[ >]", head))
    # Without Bates: no page references, and the widths still match the columns.
    bundles, plan, opts, _ = appendix_world(stamp_bates=False)
    html = report.build_index_html(bundles, plan, opts)
    assert "DOC-" not in html
    text = _text(html)
    assert "1 Attachments 01 of 01 Budget FY27.xlsx 4 1, 4" in text
    head = html.split("Attachments in the attachment volumes", 1)[1].split("</thead>", 1)[0]
    assert head.count("<col ") == len(re.findall(r"<th[ >]", head))


def test_index_appendix_carrier_list_is_capped():
    bundles, plan, opts, _ = appendix_world()
    extra = []
    for seq in range(5, 5 + report.APPENDIX_INDEX_CARRIERS + 2):
        b = MessageBundle(seq=seq, model=make_model(seq), folders=["Inbox"], thread_id="t",
                          message_pdf=Path(f"/work/msg/{seq}.pdf"), message_pages=1,
                          attachments=[make_entry(1, 1, "Budget FY27.xlsx")])
        b.attachments[0].appendix_item = 1
        extra.append(b)
    (row,) = report._appendix_index_rows(bundles + extra, plan, opts)
    assert row["messages"] == "1, 4, 5, 6, 7, 8, 9, 10, 11, 12 and 4 more"


def test_start_here_lists_appendix_volumes():
    bundles, plan, opts, _ = appendix_world()
    stats = sample_stats()
    stats.update(attachments_converted=1, attachments_crossref=0, attachments_appendix=2, appendix_items=1)
    html = report.build_start_here_html(opts, stats, plan)
    text = _text(html)
    assert ("1 attachment volume (Attachments 01 of 01.pdf). Attachments longer than 3 pages are here, so the "
            "messages stay together. A one-page note behind the message says where each one is. You can redact "
            "the message volumes first and these afterwards.") in text
    assert ("1 was converted to pages. 2 more were converted and are in the attachment volumes as 1 item, "
            "because identical copies appear there once.") in text
    assert "Pages: 19, numbered DOC-000001 to DOC-000019." in text
    assert "The attachment volumes can wait until the message volumes are done." in text
    assert ";" not in re.sub(r"&[a-z]+;", "", re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL))
    # --appendix-over 0 moves everything that became pages.
    text = _text(report.build_start_here_html(sample_options(appendix_over=0), stats, plan))
    assert "Every attachment that was converted to pages is here" in text


def test_start_here_without_appendix_says_nothing_about_it(world):
    _, plan, opts = world
    text = _text(report.build_start_here_html(opts, sample_stats(), plan))
    assert "attachment volume" not in text


def test_manifest_lists_appendix_volumes_and_items(tmp_path):
    bundles, plan, opts, _ = appendix_world()
    vol = tmp_path / "Volume 01 of 01.pdf"
    vol.write_bytes(b"%PDF-1.7 fake")
    att = tmp_path / "Attachments 01 of 01.pdf"
    att.write_bytes(b"%PDF-1.7 fake appendix")
    volumes = [Volume(1, 1, vol, PageRange(1, 14), 1, 4, 14)]
    appendix_volumes = [Volume(1, 1, att, PageRange(15, 19), 1, 1, 5)]
    stats = sample_stats()
    stats.update(attachments_appendix=2, appendix_items=1)
    out = report.write_manifest(tmp_path, opts, stats, plan, volumes, bundles=bundles, appendix_volumes=appendix_volumes)
    doc = json.loads(out.read_text())
    assert doc["options"]["appendix_over"] == 3
    assert doc["counts"]["attachments_appendix"] == 2 and doc["counts"]["appendix_items"] == 1
    assert doc["bates"]["last"] == "DOC-000019" and doc["bates"]["pages"] == 19
    (av,) = doc["appendix_volumes"]
    assert av == {
        "number": 1, "total": 1, "filename": "Attachments 01 of 01.pdf", "sha256": report.sha256_file(att),
        "pages": 5, "bates_start": "DOC-000015", "bates_end": "DOC-000019", "first_item": 1, "last_item": 1,
    }
    (item,) = doc["appendix_items"]
    assert item == {
        "item": 1, "filename": "Budget FY27.xlsx", "size": 84 * 1024, "sha256": "0" * 64, "pages": 4,
        "volume": "Attachments 01 of 01.pdf", "bates_start": "DOC-000015", "bates_end": "DOC-000019",
        "messages": [1, 4],
    }


def test_manifest_without_appendix_has_no_appendix_keys(world, tmp_path):
    bundles, plan, opts = world
    doc = json.loads(report.write_manifest(tmp_path, opts, sample_stats(), plan, [], bundles=bundles).read_text())
    assert "appendix_volumes" not in doc and "appendix_items" not in doc
    assert doc["options"]["appendix_over"] is None


@pytest.mark.parametrize(
    ("n", "sentence"),
    [
        (1, "1 message is shown as plain text because its formatting would have run text past the edge of the page, "
            "where it would be hidden from view but could still be searched and copied. Each carries a notice saying so."),
        (3, "3 messages are shown as plain text because their formatting would have run text past the edge of the page,"),
    ],
)
def test_start_here_counts_messages_shown_as_text_to_keep_them_on_the_page(world, n, sentence):
    """The reviewers should know up front, not only from the page itself,
    that some messages are not shown as they were formatted, and why."""
    _, plan, opts = world
    stats = sample_stats()
    stats.update(offpage_fallbacks=n)
    text = _text(report.build_start_here_html(opts, stats, plan))
    assert sentence in text
    assert "could not be laid out as sent" not in text  # counted apart from rendering failures
