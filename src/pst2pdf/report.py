"""Report pages: attachment placeholders, cross-references, the index, the
exceptions table, START HERE, the pre-mark detail file, and the
machine-readable manifest.

Everything visible is written for the reviewer who will page through the
volumes in Acrobat: plain words, no file paths, no hashes. The only place a
hash or a path appears is ``manifest.json``, which is for the operator.

Two things never reach a file that could be released: the text of any
pre-marked term (START HERE and the manifest carry per-category counts only,
and the per-term detail goes to a separate file whose name says DO NOT
RELEASE), and anything a reviewer has not had the chance to redact (the
exceptions table lives inside Volume 01, behind the index, so subject lines
and attachment names on it go through the same redaction pass).

Each ``render_*`` function has a ``build_*_html`` twin that returns the HTML
string; tests assert on the HTML so they run without WeasyPrint.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, decisions, natives, render, searchreport, survey
from .model import AppendixItem, AttachmentEntry, MessageBundle, NativeFile, Options, PageRange, Plan, Volume

START_HERE_NAME = "00 START HERE.pdf"
MANIFEST_NAME = "manifest.json"
WORDLIST_NAME = "Redaction word list.txt"
NATIVES_DIR_NAME = natives.NATIVES_DIR_NAME
PREMARK_DETAIL_NAME = "Pre-marked terms - DO NOT RELEASE.txt"
OTHER_CATEGORY = "other terms"  # label for pre-marked terms that carry no [category]

# How many recipients the index shows per message before "and N more".
INDEX_RECIPIENT_LIMIT = 2
# How many carrying messages an appendix item lists before "and N others":
# on its header sheet (kept to one page) and in the index.
APPENDIX_HEADER_CARRIERS = 5
APPENDIX_INDEX_CARRIERS = 10

_NAME_ADDR_RE = re.compile(r"^(.*?)\s*<([^<>]*)>\s*$")
_CATEGORY_PREFIX_RE = re.compile(r"^\[(?P<category>[^\]]*)\]\s*(?P<text>.*)$")


@dataclass
class RunStats:
    """Counts for START HERE and the manifest. cli.py fills this; every field
    has a default so a partial one still renders.

    Any mapping with the same keys is accepted wherever a RunStats is
    expected; unknown keys are ignored.
    """

    messages_found: int = 0  # mail + calendar items parsed from the archive
    messages: int = 0  # in the volumes, after dedup
    duplicates_removed: int = 0  # messages found in more than one folder and shown once
    calendar_items: int = 0
    messages_with_images: int = 0
    attachments: int = 0  # attachment slots across all messages in the volumes
    attachments_converted: int = 0  # pages produced (includes re-saved PDFs)
    attachments_crossref: int = 0  # identical to an earlier one, replaced by a note
    attachments_exceptions: int = 0  # placeholder pages
    attachments_withheld: int = 0  # earlier copies withheld by a latest decision (--decisions), a note in their place
    attachments_omitted: int = 0  # kept out by an omit decision (--decisions), a note with the reviewers' reason in their place
    attachments_containers: int = 0  # zip archives whose files follow as attachments of their own
    attachments_native: int = 0  # --triage: slots written as original files, a pointer page in their place
    native_files: int = 0  # --triage: distinct files written
    native_bytes: int = 0  # --triage: their total size
    attachments_appendix: int = 0  # slots whose pages are in the appendix volumes (--appendix-over), a pointer page in their place
    appendix_items: int = 0  # distinct items in the appendix volumes
    skipped_non_mail: int = 0
    rtf_fallbacks: int = 0
    render_fallbacks: int = 0  # messages shown unformatted or header-only because rendering failed
    offpage_fallbacks: int = 0  # messages shown unformatted because their formatted page put text outside the page
    offpage_text: int = 0  # renders (attachments, report pages) that left text or images outside the page; should be 0, the -v log names them
    blocked_urls: int = 0
    errors: int = 0
    started: datetime | None = None
    finished: datetime | None = None
    coverage: Any = None  # premark.Coverage (or equivalent mapping) when pre-marking ran; never released
    coverage_summary: Any = None  # summarize_coverage() output; what START HERE and the manifest show
    terms_file_sha256: str | None = None
    intake_exceptions: int = 0  # MBOX messages with intake warnings or parsing failures
    render_failures: list[dict] = field(default_factory=list)  # [{"message": seq, "reason": ...}] header-only pages
    exceptions: list[dict] = field(default_factory=list)  # optional; write_manifest derives from bundles when given
    decisions: Any = None  # --decisions: the manifest's record of every rule and what it did
    page_budget: Any = None  # model.PageBudget from survey.measure: START HERE's largest attachments, the manifest's


def _as_stats(stats: Any) -> RunStats:
    if isinstance(stats, RunStats):
        return stats
    if stats is None:
        return RunStats()
    names = {f.name for f in fields(RunStats)}
    if isinstance(stats, Mapping):
        return RunStats(**{k: v for k, v in stats.items() if k in names})
    return RunStats(**{n: getattr(stats, n) for n in names if hasattr(stats, n)})


# --- wording helpers -------------------------------------------------------

# extension -> (what the reviewer sees as the type, the "converted from X" source)
_TYPES: dict[str, tuple[str, str]] = {
    "doc": ("Word document", "Word"),
    "docx": ("Word document", "Word"),
    "docm": ("Word document", "Word"),
    "dot": ("Word template", "Word"),
    "dotx": ("Word template", "Word"),
    "odt": ("OpenDocument text", "an OpenDocument file"),
    "rtf": ("Rich text document", "rich text"),
    "wpd": ("WordPerfect document", "WordPerfect"),
    "xls": ("Excel spreadsheet", "Excel"),
    "xlsx": ("Excel spreadsheet", "Excel"),
    "xlsm": ("Excel spreadsheet", "Excel"),
    "ods": ("OpenDocument spreadsheet", "an OpenDocument file"),
    "ppt": ("PowerPoint presentation", "PowerPoint"),
    "pptx": ("PowerPoint presentation", "PowerPoint"),
    "odp": ("OpenDocument presentation", "an OpenDocument file"),
    "pdf": ("PDF document", "PDF"),
    "png": ("PNG image", "an image"),
    "jpg": ("JPEG image", "an image"),
    "jpeg": ("JPEG image", "an image"),
    "gif": ("GIF image", "an image"),
    "bmp": ("Bitmap image", "an image"),
    "tif": ("TIFF image", "an image"),
    "tiff": ("TIFF image", "an image"),
    "webp": ("WebP image", "an image"),
    "heic": ("HEIC image", "an image"),
    "txt": ("Text file", "plain text"),
    "log": ("Text file", "plain text"),
    "md": ("Text file", "plain text"),
    "json": ("Text file", "plain text"),
    "xml": ("Text file", "plain text"),
    "csv": ("CSV spreadsheet", "a CSV file"),
    "html": ("Web page", "a web page"),
    "htm": ("Web page", "a web page"),
    "eml": ("Email message", "an email message"),
    "msg": ("Email message", "an email message"),
    "ics": ("Calendar item", "a calendar item"),
    "zip": ("Zip archive", "a zip archive"),
}

_METHOD_SOURCE = {
    "libreoffice": "an Office document",
    "image": "an image",
    "text": "plain text",
    "html": "a web page",
    "message": "an email message",
    "calendar": "a calendar item",
}


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def describe_type(filename: str, content_type: str = "") -> str:
    """'Budget FY27.xlsx' -> 'Excel spreadsheet'. Falls back to the media type family."""
    known = _TYPES.get(_ext(filename))
    if known:
        return known[0]
    major = (content_type or "").split("/")[0].lower()
    if major == "image":
        return "Image"
    if major == "text":
        return "Text file"
    if major == "audio":
        return "Audio file"
    if major == "video":
        return "Video file"
    return "File"


def _sentence_case(text: str) -> str:
    """Capitalize the first letter only, so a name such as --withheld-by's keeps its capitals."""
    return text[:1].upper() + text[1:]


def describe_method(entry: AttachmentEntry, withheld_by: str = "") -> str:
    """One phrase for the cover sheet: 'Converted from Excel', 'Original PDF pages', ..."""
    conv = entry.converted
    if conv is None:
        return "Identical to an earlier attachment"
    if conv.status == "placeholder":
        return "Not converted"
    if conv.status == "withheld":
        return "Not included" if conv.decision == "omit" else _sentence_case(decisions.withheld_at(withheld_by))
    if conv.status == "container":
        return "Zip archive. Its files follow as attachments of their own"
    if conv.status == "native":
        return "Not converted: provided as the original file"
    if conv.method == "pdf":
        return "Original PDF pages, re-saved without scripts, forms, or embedded files"
    known = _TYPES.get(_ext(entry.filename))
    source = known[1] if known else _METHOD_SOURCE.get(conv.method, "the original file")
    return f"Converted from {source}"


def _pages_phrase(n: int) -> str:
    return "1 page" if n == 1 else f"{n:,} pages"


def _long_date(dt: datetime | None) -> str:
    """'Tue, 16 Sep 2025 10:17 CDT'. Naive datetimes get no zone."""
    return dt.strftime("%a, %d %b %Y %H:%M %Z").strip() if dt else "(unknown)"


def _short_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "(undated)"


def _day(dt: datetime | None) -> str:
    dt = dt or datetime.now()
    return f"{dt:%B} {dt.day}, {dt:%Y}"


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def _names(header: str, limit: int | None = None) -> str:
    """Display names only (address when there is no name), for the index."""
    people: list[str] = []
    for piece in render.split_addresses(header):
        m = _NAME_ADDR_RE.match(piece)
        people.append((m.group(1).strip() or m.group(2).strip()) if m else piece)
    people = [p for p in people if p]
    if limit and len(people) > limit:
        rest = len(people) - limit
        return ", ".join(people[:limit]) + f" and {rest} more"
    return ", ".join(people)


def _range_text(spec, rng: PageRange | None) -> str:
    if rng is None:
        return ""
    if rng.start == rng.end:
        return spec.label(rng.start)
    return f"{spec.label(rng.start)} to {spec.label(rng.end)}"


def _range_compact(spec, rng: PageRange | None) -> str:
    """'DOC-000049 to 000050': the shared prefix is printed once, for the index."""
    if rng is None:
        return ""
    if rng.start == rng.end:
        return spec.label(rng.start)
    end = spec.label(rng.end)
    if spec.prefix and end.startswith(spec.prefix):
        end = end[len(spec.prefix):]
    return f"{spec.label(rng.start)} to {end}"


def _overall_range(plan: Plan | None) -> PageRange | None:
    if plan is None:
        return None
    volumes = [*plan.volumes, *plan.appendix_volumes]
    starts = [v.bates.start for v in volumes]
    ends = [v.bates.end for v in volumes]
    if plan.front_matter:
        starts.append(plan.front_matter.start)
        ends.append(plan.front_matter.end)
    if not starts:
        return None
    return PageRange(min(starts), max(ends))


def volume_for(plan: Plan | None, number: int | None) -> Volume | None:
    """The planned volume whose Bates range holds *number*, or None."""
    if plan is None or number is None:
        return None
    for v in plan.volumes:
        if v.bates.start <= number <= v.bates.end:
            return v
    return None


def volume_name(plan: Plan | None, number: int | None) -> str:
    """'Volume 05 of 08.pdf' for a Bates number, '' when it is not in the plan."""
    v = volume_for(plan, number)
    return Path(v.path).name if v else ""


def appendix_volume_for(plan: Plan | None, item: int | None) -> Volume | None:
    """The planned appendix volume holding appendix item *item*, or None."""
    if plan is None or item is None:
        return None
    for v in plan.appendix_volumes:
        if v.first_seq <= item <= v.last_seq:
            return v
    return None


def appendix_volume_name(plan: Plan | None, item: int | None) -> str:
    """'Attachments 01 of 02.pdf' for an appendix item, '' when it is not in the plan."""
    v = appendix_volume_for(plan, item)
    return Path(v.path).name if v else ""


def _message_ref(bundle: MessageBundle) -> dict[str, Any]:
    m = bundle.model
    return {
        "seq": bundle.seq,
        "date": _long_date(m.date),
        "dated": m.date is not None,
        "subject": m.subject or "(no subject)",
        "kind": m.kind,
    }


def _attachment_ref(entry: AttachmentEntry, withheld_by: str = "") -> dict[str, Any]:
    conv = entry.converted
    return {
        "index": entry.index,
        "total": entry.total,
        "filename": entry.filename,
        "size": render.human_size(entry.size),
        "type": describe_type(entry.filename, entry.content_type),
        "method": describe_method(entry, withheld_by),
        "pages": _pages_phrase(conv.page_count) if conv and conv.page_count else "",
        "reason": (conv.reason or "").strip() if conv else "",
    }


# --- per-attachment pages ---------------------------------------------------


def build_placeholder_html(entry: AttachmentEntry, bundle: MessageBundle, options: Options) -> str:
    att = _attachment_ref(entry, options.withheld_by)
    reason = att["reason"] or "it is in a format the conversion tool does not handle"
    return render.build_template(
        "placeholder.html",
        att=att,
        msg=_message_ref(bundle),
        reason=reason[0].lower() + reason[1:] if reason else reason,
        listed=options.convert_attachments,
        keep_natives=options.keep_natives,
        natives_dir=NATIVES_DIR_NAME,
        request_id=options.request_id,
    )


def build_native_pointer_html(entry: AttachmentEntry, bundle: MessageBundle, file: NativeFile, options: Options) -> str:
    """--triage: the page standing where an attachment was, naming the
    original file it was written as."""
    return render.build_template(
        "native_pointer.html",
        att=_attachment_ref(entry, options.withheld_by),
        msg=_message_ref(bundle),
        file_id=file.id,
        file_name=file.file_name,
        natives_dir=natives.NATIVES_DIR_NAME,
        messages=len({seq for seq, _ in file.carriers}),
        request_id=options.request_id,
    )


def build_withheld_html(entry: AttachmentEntry, bundle: MessageBundle, options: Options) -> str:
    """The note standing in for an attachment withheld by a decision.
    An omitted attachment's reason is the reviewers' note, printed as written."""
    att = _attachment_ref(entry, options.withheld_by)
    reason = att["reason"]
    omit = entry.converted is not None and entry.converted.decision == "omit"
    return render.build_template(
        "withheld.html",
        att=att,
        msg=_message_ref(bundle),
        reason=reason if omit or not reason else reason[0].lower() + reason[1:],
        omit=omit,
        request_id=options.request_id,
    )


def render_placeholder(entry: AttachmentEntry, bundle: MessageBundle, options: Options, out: Path) -> Path:
    """One page standing in for an attachment that could not be converted."""
    out = Path(out)
    render.render_checked(build_placeholder_html(entry, bundle, options), out)
    return out


def container_children(entry: AttachmentEntry, bundle: MessageBundle) -> list[AttachmentEntry]:
    """The attachments that came out of zip *entry*: those named ``<zip>/<file>``."""
    prefix = entry.filename + "/"
    return [e for e in bundle.attachments if e.index != entry.index and e.filename.startswith(prefix)]


def build_container_html(entry: AttachmentEntry, bundle: MessageBundle, options: Options) -> str:
    """Cover sheet for a zip archive whose files follow as attachments of their own."""
    children = container_children(entry, bundle)
    indexes = [c.index for c in children]
    conv = entry.converted
    skipped = [line.strip() for line in (conv.reason if conv else "").splitlines() if line.strip()]
    return render.build_template(
        "container.html",
        att=_attachment_ref(entry, options.withheld_by),
        msg=_message_ref(bundle),
        files=len(children),
        first_child=min(indexes) if indexes else 0,
        last_child=max(indexes) if indexes else 0,
        skipped=skipped,
        request_id=options.request_id,
    )


def render_container(entry: AttachmentEntry, bundle: MessageBundle, options: Options, out: Path) -> Path:
    out = Path(out)
    render.render_checked(build_container_html(entry, bundle, options), out)
    return out


def build_crossref_html(
    entry: AttachmentEntry,
    bundle: MessageBundle,
    first_bundle: MessageBundle,
    first_range: PageRange,
    options: Options,
    plan: Plan | None = None,
) -> str:
    return render.build_template(
        "crossref.html",
        att=_attachment_ref(entry, options.withheld_by),
        msg=_message_ref(bundle),
        first=_message_ref(first_bundle),
        bates=options.stamp_bates,
        first_range=_range_text(options.bates, first_range) if options.stamp_bates else "",
        first_volume=volume_name(plan, first_range.start if first_range else None),
        request_id=options.request_id,
    )


def render_crossref(
    entry: AttachmentEntry,
    bundle: MessageBundle,
    first_bundle: MessageBundle,
    first_range: PageRange,
    options: Options,
    out: Path,
    plan: Plan | None = None,
) -> Path:
    """One page pointing at the first copy of an identical attachment."""
    out = Path(out)
    render.render_checked(build_crossref_html(entry, bundle, first_bundle, first_range, options, plan), out)
    return out


# --- appendix (--appendix-over) ---------------------------------------------


def _carrier_messages(carriers: list[tuple[int, int]]) -> list[tuple[int, list[int]]]:
    """[(seq, [attachment indexes])] in first-seen order."""
    by_seq: dict[int, list[int]] = {}
    for seq, index in carriers:
        by_seq.setdefault(seq, []).append(index)
    return list(by_seq.items())


def _and_list(numbers: list[int]) -> str:
    words = [str(n) for n in numbers]
    if len(words) <= 2:
        return " and ".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def build_appendix_pointer_html(
    entry: AttachmentEntry,
    bundle: MessageBundle,
    item: AppendixItem,
    plan: Plan | None,
    options: Options,
) -> str:
    """The page standing in for an attachment moved to the appendix."""
    rng = plan.appendix_ranges.get(item.number) if plan else None
    return render.build_template(
        "appendix_pointer.html",
        att=_attachment_ref(entry, options.withheld_by),
        msg=_message_ref(bundle),
        item=item.number,
        pages=_pages_phrase(item.converted.page_count) if item.converted.page_count else "",
        messages=len(_carrier_messages(item.carriers)),
        bates=options.stamp_bates,
        item_range=_range_text(options.bates, rng) if options.stamp_bates else "",
        volume=appendix_volume_name(plan, item.number),
        over=options.appendix_over or 0,
        request_id=options.request_id,
    )


def render_appendix_pointer(
    entry: AttachmentEntry,
    bundle: MessageBundle,
    item: AppendixItem,
    plan: Plan | None,
    options: Options,
    out: Path,
) -> Path:
    """One page pointing at the appendix item that holds this attachment's pages."""
    out = Path(out)
    render.render_checked(build_appendix_pointer_html(entry, bundle, item, plan, options), out)
    return out


def build_appendix_header_html(
    item: AppendixItem, bundles_by_seq: Mapping[int, MessageBundle], options: Options
) -> str:
    """The one-page sheet in front of an appendix item. It carries no page
    references, so it is rendered once, before planning."""
    carriers = _carrier_messages(item.carriers)
    shown = []
    for seq, indexes in carriers[:APPENDIX_HEADER_CARRIERS]:
        bundle = bundles_by_seq.get(seq)
        shown.append(
            {
                "date": _long_date(bundle.model.date) if bundle else "",
                "subject": (bundle.model.subject or "(no subject)") if bundle else "",
                "attachments": ("attachment " if len(indexes) == 1 else "attachments ") + _and_list(indexes),
            }
        )
    conv = item.converted
    entry = AttachmentEntry(
        index=0, total=0, filename=item.filename, size=item.size,
        content_type=item.content_type, sha256=item.sha256, converted=conv,
    )
    return render.build_template(
        "appendix_item.html",
        item=item.number,
        filename=item.filename,
        size=render.human_size(item.size),
        type=describe_type(item.filename, item.content_type),
        method=describe_method(entry, options.withheld_by),
        pages=_pages_phrase(conv.page_count) if conv.page_count else "",
        messages=len(carriers),
        carriers=shown,
        others=max(0, len(carriers) - len(shown)),
        request_id=options.request_id,
    )


def render_appendix_header(
    item: AppendixItem, bundles_by_seq: Mapping[int, MessageBundle], options: Options, out: Path
) -> Path:
    out = Path(out)
    render.render_checked(build_appendix_header_html(item, bundles_by_seq, options), out)
    return out


def appendix_rows(bundles: list[MessageBundle], plan: Plan | None, options: Options) -> list[dict[str, Any]]:
    """One row per appendix item, derived from the bundles' pointer slots, for
    the index and the manifest."""
    rows: dict[int, dict[str, Any]] = {}
    for b in sorted(bundles, key=lambda x: x.seq):
        for e in b.attachments:
            if e.appendix_item is None:
                continue
            row = rows.get(e.appendix_item)
            if row is None:
                conv = e.converted
                rng = plan.appendix_ranges.get(e.appendix_item) if plan else None
                row = rows[e.appendix_item] = {
                    "item": e.appendix_item,
                    "filename": e.filename,
                    "size": e.size,
                    "sha256": e.sha256,
                    "type": describe_type(e.filename, e.content_type),
                    "pages": conv.page_count if conv else 0,
                    "range": rng,
                    "volume": appendix_volume_name(plan, e.appendix_item),
                    "messages": [],
                }
            if b.seq not in row["messages"]:
                row["messages"].append(b.seq)
    return [rows[n] for n in sorted(rows)]


def _appendix_index_rows(bundles: list[MessageBundle], plan: Plan, options: Options) -> list[dict[str, Any]]:
    out = []
    for row in appendix_rows(bundles, plan, options):
        seqs = row["messages"]
        shown = ", ".join(str(n) for n in seqs[:APPENDIX_INDEX_CARRIERS])
        if len(seqs) > APPENDIX_INDEX_CARRIERS:
            shown += f" and {len(seqs) - APPENDIX_INDEX_CARRIERS} more"
        out.append(
            {
                "item": row["item"],
                "range": _range_compact(options.bates, row["range"]),
                "volume": row["volume"][: -len(".pdf")] if row["volume"] else "",
                "filename": row["filename"],
                "pages": row["pages"],
                "messages": shown,
            }
        )
    return out


def _appendix_volume_rows(plan: Plan, options: Options) -> list[dict[str, Any]]:
    return [
        {
            "name": Path(v.path).name[: -len(".pdf")],
            "range": _range_text(options.bates, v.bates),
            "pages": v.page_count or (v.bates.end - v.bates.start + 1),
            "first_item": v.first_seq,
            "last_item": v.last_seq,
        }
        for v in plan.appendix_volumes
    ]


# Appendix table in the index: Item, [Pages], Volume, Attachment, Length, Messages.
_APPENDIX_WIDTHS: dict[bool, list[float]] = {
    True: [5, 14, 15, 36, 8, 22],
    False: [5, 17, 46, 8, 24],
}


# --- index ------------------------------------------------------------------


# Index table column widths as percentages, in header order, keyed by
# (Pages column shown, Folder column shown). The table is fixed-layout, so
# each set has to name every column it has and add up to 100.
_INDEX_WIDTHS: dict[tuple[bool, bool], list[float]] = {
    # #    Pages  Date  From  To    Subject Folder Att.  Pict.
    (True, True): [3.5, 12.5, 9.7, 13, 17, 26.3, 11, 3.5, 3.5],
    (True, False): [3.5, 12.5, 9.7, 15, 19, 33.3, 3.5, 3.5],
    (False, True): [3.5, 9.7, 16, 20.5, 31.3, 12, 3.5, 3.5],
    (False, False): [3.5, 9.7, 18, 22.5, 39.3, 3.5, 3.5],
}

# Exceptions table, same idea: Msg., [Page], Volume, Date, Attachment, Size, Type, Reason.
_EXCEPTION_WIDTHS: dict[bool, list[float]] = {
    True: [5, 10, 13, 11, 26, 7, 11, 17],
    False: [5, 14, 12, 30, 7, 12, 20],
}


def _volume_rows(plan: Plan, options: Options) -> list[dict[str, Any]]:
    rows = []
    for v in plan.volumes:
        rows.append(
            {
                "number": v.number,
                "total": v.total,
                "name": Path(v.path).stem,
                "range": _range_text(options.bates, v.bates),
                "pages": v.page_count or (v.bates.end - v.bates.start + 1),
                "first_seq": v.first_seq,
                "last_seq": v.last_seq,
            }
        )
    return rows


def has_picture(bundle: MessageBundle) -> bool:
    """The index's Pict. rule: embedded images or an image attachment.
    cli.py counts ``messages_with_images`` with this same rule."""
    if bundle.model.has_images:
        return True
    return any(e.converted is not None and e.converted.method == "image" for e in bundle.attachments)


_has_picture = has_picture


def _folder_text(bundle: MessageBundle) -> str:
    return ", ".join(bundle.folders) if bundle.folders else (bundle.model.folder or "")


def _index_rows(bundles: list[MessageBundle], plan: Plan, options: Options) -> list[dict[str, Any]]:
    rows = []
    for b in sorted(bundles, key=lambda x: x.seq):
        rng = plan.bundle_ranges.get(b.seq)
        m = b.model
        rows.append(
            {
                "seq": b.seq,
                "bates_start": options.bates.label(rng.start) if rng else "",
                "bates_end": options.bates.label(rng.end) if rng and rng.end != rng.start else "",
                "range": _range_compact(options.bates, rng),
                "date": _short_date(m.date),
                "from": _names(m.from_addr),
                "to": _names(m.to_addrs, INDEX_RECIPIENT_LIMIT),
                "subject": m.subject or "(no subject)",
                "kind": m.kind,
                "folders": _folder_text(b),
                "attachments": len(b.attachments),
                "images": has_picture(b),
            }
        )
    return rows


def common_folder(bundles: list[MessageBundle]) -> str | None:
    """The one folder every message came from, or None when they differ."""
    folders = {_folder_text(b) for b in bundles}
    if len(folders) == 1:
        (only,) = folders
        return only or None
    return None


def build_index_html(
    bundles: list[MessageBundle], plan: Plan, options: Options, prepared: datetime | None = None
) -> str:
    overall = _overall_range(plan)
    folder = common_folder(bundles)
    return render.build_template(
        "index.html",
        request_id=options.request_id,
        prepared=_day(prepared),
        message_count=len(bundles),
        volume_count=len(plan.volumes),
        page_count=(overall.end - overall.start + 1) if overall else 0,
        bates=options.stamp_bates,
        overall_range=_range_text(options.bates, overall),
        volumes=_volume_rows(plan, options),
        rows=_index_rows(bundles, plan, options),
        common_folder=folder,
        col_widths=_INDEX_WIDTHS[(options.stamp_bates, folder is None)],
        images_flagged=sum(1 for b in bundles if has_picture(b)),
        appendix_volumes=_appendix_volume_rows(plan, options),
        appendix_rows=_appendix_index_rows(bundles, plan, options),
        appendix_widths=_APPENDIX_WIDTHS[options.stamp_bates],
    )


def render_index(
    bundles: list[MessageBundle], plan: Plan, options: Options, out: Path, prepared: datetime | None = None
) -> Path:
    """Landscape table of every message with its page range; goes at the front of Volume 01."""
    out = Path(out)
    render.render_checked(build_index_html(bundles, plan, options, prepared), out)
    return out


# --- pre-mark coverage ------------------------------------------------------


def _cov_get(cov: Any, *names):
    for n in names:
        if isinstance(cov, Mapping) and n in cov:
            return cov[n]
        if hasattr(cov, n):
            return getattr(cov, n)
    return None


def _term_rows(value: Any) -> list[tuple[str, str | None, int]]:
    """Normalize term hits into (text, category or None, hits)."""
    if not value:
        return []

    def split(key: Any) -> tuple[str, str | None]:
        if isinstance(key, str):
            m = _CATEGORY_PREFIX_RE.match(key)
            if m:
                return m.group("text").strip(), (m.group("category").strip() or None)
            return key, None
        text = getattr(key, "text", None) or (key.get("text") if isinstance(key, Mapping) else None) or str(key)
        cat = getattr(key, "category", None) or (key.get("category") if isinstance(key, Mapping) else None)
        return str(text), (str(cat) if cat else None)

    out: list[tuple[str, str | None, int]] = []
    if isinstance(value, Mapping):
        for k, v in value.items():
            text, cat = split(k)
            out.append((text, cat, int(v)))
        return out
    for item in value:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            text, cat = split(item[0])
            out.append((text, cat, int(item[1])))
        else:
            hits = getattr(item, "hits", None)
            if hits is None and isinstance(item, Mapping):
                hits = item.get("hits", 0)
            text, cat = split(item)
            out.append((text, cat, int(hits or 0)))
    return out


def summarize_coverage(cov: Any, terms: Any = None) -> dict[str, Any] | None:
    """Per-category totals from what premark returned. Never includes a term's text.

    *cov* is a ``premark.Coverage`` (``term_hits`` keyed by term text) or a
    mapping whose keys may carry a ``[category]`` prefix. *terms* is the
    ``premark.Term`` list the run used, which supplies categories when the
    coverage object does not.
    """
    if cov is None:
        return None
    rows = _term_rows(_cov_get(cov, "term_hits", "terms"))
    patterns = _term_rows(_cov_get(cov, "pattern_hits", "patterns"))
    if not rows and not patterns:
        return None
    by_text: dict[str, str | None] = {}
    for t in terms or []:
        text = getattr(t, "text", None)
        if text is not None and not getattr(t, "is_pattern", False):
            by_text.setdefault(str(text).casefold(), getattr(t, "category", None))
    categories: dict[str, dict[str, int]] = {}
    for text, cat, hits in rows:
        cat = cat or by_text.get(text.casefold()) or OTHER_CATEGORY
        row = categories.setdefault(cat, {"terms": 0, "marks": 0, "terms_without_hits": 0})
        row["terms"] += 1
        row["marks"] += hits
        if hits == 0:
            row["terms_without_hits"] += 1
    return {
        "categories": [{"category": c, **v} for c, v in categories.items()],
        "patterns": [{"pattern": name, "marks": hits} for name, _cat, hits in patterns],
        "terms": len(rows),
        "terms_without_hits": sum(1 for _t, _c, h in rows if h == 0),
        "total": sum(h for _t, _c, h in rows) + sum(h for _n, _c, h in patterns),
    }


def build_premark_detail(cov: Any, terms: Any = None) -> str:
    """Per-term text for the DO NOT RELEASE file: every term, its category,
    its marks, and the searched phrases when the coverage reports them."""
    rows = _term_rows(_cov_get(cov, "term_hits", "terms"))
    patterns = _term_rows(_cov_get(cov, "pattern_hits", "patterns"))
    phrase_hits = _cov_get(cov, "phrase_hits") or {}
    by_text: dict[str, Any] = {}
    for t in terms or []:
        text = getattr(t, "text", None)
        if text is not None:
            by_text.setdefault(str(text).casefold(), t)
    lines = [
        "PRE-MARKED TERMS. DO NOT RELEASE.",
        "This file lists the names and terms that were marked for redaction in the volumes.",
        "It exists as a record of what was searched for.",
        "It must stay with the request's working files and never be sent to a requestor.",
        "",
    ]
    for text, cat, hits in rows:
        term = by_text.get(text.casefold())
        cat = cat or (getattr(term, "category", None) if term else None)
        label = f"[{cat}] {text}" if cat else text
        lines.append(f"{label}: {hits:,} mark{'' if hits == 1 else 's'}" + ("" if hits else " (no matches)"))
        if term is not None and hasattr(term, "phrases"):
            for phrase in term.phrases():
                if phrase == text:
                    continue
                n = phrase_hits.get(phrase) if isinstance(phrase_hits, Mapping) else None
                lines.append(f"    also searched: {phrase}" + (f" ({n:,})" if n is not None else ""))
    for name, _cat, hits in patterns:
        lines.append(f"[pattern] {name}: {hits:,} mark{'' if hits == 1 else 's'}")
    return "\r\n".join(lines) + "\r\n"


def write_premark_detail(output_dir: Path, cov: Any, terms: Any = None) -> Path | None:
    """The per-term file. None when pre-marking did not run."""
    if cov is None:
        return None
    out = Path(output_dir) / PREMARK_DETAIL_NAME
    out.write_bytes(build_premark_detail(cov, terms).encode("utf-8"))
    return out


# --- START HERE -------------------------------------------------------------


def _counts(stats: RunStats, plan: Plan | None, options: Options) -> dict[str, Any]:
    overall = _overall_range(plan)
    pages = (overall.end - overall.start + 1) if overall else 0
    return {
        "messages": stats.messages,
        "messages_found": stats.messages_found,
        "duplicates_removed": stats.duplicates_removed,
        "calendar_items": stats.calendar_items,
        "messages_with_images": stats.messages_with_images,
        "attachments": stats.attachments,
        "attachments_converted": stats.attachments_converted,
        "attachments_crossref": stats.attachments_crossref,
        "attachments_exceptions": stats.attachments_exceptions,
        "attachments_withheld": stats.attachments_withheld,
        "attachments_omitted": stats.attachments_omitted,
        "attachments_containers": stats.attachments_containers,
        "attachments_appendix": stats.attachments_appendix,
        "appendix_items": stats.appendix_items,
        "skipped_non_mail": stats.skipped_non_mail,
        "rtf_fallbacks": stats.rtf_fallbacks,
        "render_fallbacks": stats.render_fallbacks,
        "offpage_fallbacks": stats.offpage_fallbacks,
        "render_failures": len(stats.render_failures or []),
        "intake_exceptions": stats.intake_exceptions,
        "blocked_urls": stats.blocked_urls,
        "volumes": len(plan.volumes) if plan else 0,
        "pages": pages,
        "range": _range_text(options.bates, overall) if options.stamp_bates else "",
        "example_label": options.bates.label(overall.start if overall else options.bates.start),
    }


def build_start_here_html(options: Options, stats: Any, plan: Plan | None) -> str:
    st = _as_stats(stats)
    volumes = _volume_rows(plan, options) if plan else []
    coverage = st.coverage_summary if st.coverage_summary is not None else summarize_coverage(st.coverage)
    counts = _counts(st, plan, options)
    exceptions_listed = (
        (counts["attachments_exceptions"] if options.convert_attachments else 0)
        + counts["attachments_withheld"]
        + counts["attachments_omitted"]
        + counts["render_failures"]
        + counts["intake_exceptions"]
    )
    return render.build_template(
        "start_here.html",
        request_id=options.request_id,
        prepared=_day(st.started or st.finished),
        bates=options.stamp_bates,
        counts=counts,
        volumes=volumes,
        first_volume=volumes[0]["name"] + ".pdf" if volumes else "Volume 01 of 01.pdf",
        wordlist_name=WORDLIST_NAME,
        manifest_name=MANIFEST_NAME,
        premark_detail_name=PREMARK_DETAIL_NAME,
        natives_dir=NATIVES_DIR_NAME,
        keep_natives=options.keep_natives,
        convert_attachments=options.convert_attachments,
        exceptions_listed=exceptions_listed,
        order=options.order,
        dedup_messages=options.dedup_messages,
        dedup_attachments=options.dedup_attachments,
        coverage=coverage,
        appendix_volumes=_appendix_volume_rows(plan, options) if plan else [],
        appendix_over=options.appendix_over,
        largest=survey.start_here_context(st.page_budget),
        search=searchreport.context(options.search_report),
        options=options,
    )


def build_start_here_triage_html(options: Options, stats: Any, plan: Plan | None, files: list[NativeFile]) -> str:
    """--triage: what the folder is for and what the reviewers do with the index."""
    st = _as_stats(stats)
    counts = _counts(st, plan, options)
    by_type: dict[str, list[int]] = {}
    for f in files:
        if f.file_name:
            row = by_type.setdefault(natives.KIND_LABELS.get(f.kind, natives.KIND_LABELS[""]), [0, 0])
            row[0] += 1
            row[1] += f.size
    volumes = _volume_rows(plan, options) if plan else []
    return render.build_template(
        "start_here_triage.html",
        request_id=options.request_id,
        prepared=_day(st.started or st.finished),
        counts=counts,
        volumes=volumes,
        first_volume=volumes[0]["name"] + ".pdf" if volumes else "",
        natives_dir=natives.NATIVES_DIR_NAME,
        index_name=natives.INDEX_CSV_NAME,
        manifest_name=MANIFEST_NAME,
        files=sum(1 for f in files if f.file_name),
        total_size=render.human_size(sum(f.size for f in files if f.file_name)),
        by_type=[
            {"type": t, "files": n, "size": render.human_size(b)}
            for t, (n, b) in sorted(by_type.items(), key=lambda kv: (-kv[1][0], kv[0]))
        ],
        decisions=bool(options.decisions_file),
        order=options.order,
        search=searchreport.context(options.search_report),
        options=options,
    )


def write_start_here(
    output_dir: Path, options: Options, stats: Any, plan: Plan | None, files: list[NativeFile] | None = None
) -> Path:
    """The one-page guide the reviewer reads first."""
    out = Path(output_dir) / START_HERE_NAME
    if options.triage:
        html = build_start_here_triage_html(options, stats, plan, files or [])
    else:
        html = build_start_here_html(options, stats, plan)
    render.render_checked(html, out)
    return out


# --- exceptions -------------------------------------------------------------


@dataclass
class ExceptionRow:
    seq: int
    index: int  # attachment index, 0 for a message whose body could not be rendered
    filename: str
    size: int
    sha256: str
    content_type: str
    reason: str
    bates: str  # label of the placeholder (or message) page, "" when unplanned
    volume: str  # "Volume 01 of 04.pdf", "" when unplanned
    date: datetime | None
    subject: str
    withheld: bool = False  # by a decision, not a conversion failure
    decision: str = ""  # when withheld: "latest" or "omit"
    count: int = 1  # withheld copies this row stands for (grouped for the table)

    @property
    def is_message(self) -> bool:
        return self.index == 0


def list_exceptions(
    bundles: list[MessageBundle],
    plan: Plan | None,
    options: Options,
    render_failures: list[dict] | None = None,
) -> list[ExceptionRow]:
    """Every attachment that got a placeholder instead of pages, and every
    message whose body could not be rendered at all, in seq order.

    Zip containers (their files follow as attachments) are not exceptions.
    When attachment conversion was turned off for the run, no attachment is
    an exception either: START HERE says so instead.
    """
    failed = {}
    for f in render_failures or []:
        seq = f.get("message") if isinstance(f, Mapping) else getattr(f, "message", None)
        if seq is not None:
            failed[int(seq)] = (f.get("reason") if isinstance(f, Mapping) else getattr(f, "reason", "")) or ""
    rows: list[ExceptionRow] = []
    for b in sorted(bundles, key=lambda x: x.seq):
        if b.model.intake_issues:
            reasons = [failed[b.seq]] if b.seq in failed else []
            failed[b.seq] = "; ".join(reasons + b.model.intake_issues)
        if b.seq in failed:
            rng = plan.message_ranges.get(b.seq) if plan else None
            rows.append(
                ExceptionRow(
                    seq=b.seq,
                    index=0,
                    filename="",
                    size=0,
                    sha256="",
                    content_type="",
                    reason=_first_line(failed[b.seq]) or "the message body could not be rendered",
                    bates=options.bates.label(rng.start) if rng else "",
                    volume=volume_name(plan, rng.start if rng else None),
                    date=b.model.date,
                    subject=b.model.subject or "(no subject)",
                )
            )
        for e in b.attachments:
            status = e.converted.status if e.converted is not None else ""
            if not (status == "withheld" or (status == "placeholder" and options.convert_attachments)):
                continue
            rng = plan.attachment_ranges.get((b.seq, e.index)) if plan else None
            rows.append(
                ExceptionRow(
                    seq=b.seq,
                    index=e.index,
                    filename=e.filename,
                    size=e.size,
                    sha256=e.sha256,
                    content_type=e.content_type,
                    reason=_first_line(e.converted.summary or e.converted.reason) or "unsupported file type",
                    bates=options.bates.label(rng.start) if rng else "",
                    volume=volume_name(plan, rng.start if rng else None),
                    date=b.model.date,
                    subject=b.model.subject or "(no subject)",
                    withheld=status == "withheld",
                    decision=e.converted.decision if status == "withheld" else "",
                )
            )
    return rows


def group_withheld(rows: list[ExceptionRow]) -> list[ExceptionRow]:
    """One row per recurring report for the exceptions table, in place of
    one per withheld copy: hundreds of withheld copies of a few daily reports
    would otherwise fill pages of near-identical rows. Each copy keeps its own note page
    where it was attached, and the manifest still lists every copy. Rows of a
    report share the one-line summary (it names the kept copy); the grouped
    row sits where the first copy was. Omitted attachments group the same way
    by the reviewers' note, so one note on hundreds of log files is one row."""
    groups: dict[str, list[ExceptionRow]] = {}
    for r in rows:
        if r.withheld:
            groups.setdefault(r.reason, []).append(r)
    out: list[ExceptionRow] = []
    for r in rows:
        if not r.withheld:
            out.append(r)
            continue
        members = groups.get(r.reason)
        if members is None or members[0] is not r:
            continue  # folded into its report's row
        seqs = [m.seq for m in members]
        where = f"message {seqs[0]}" if len(seqs) == 1 else f"messages {min(seqs)} to {max(seqs)}"
        if r.decision == "omit":
            if len(members) > 1:
                _, _, note = r.reason.partition(": ")
                r = replace(
                    r,
                    count=len(members),
                    size=0,
                    reason=f"Not included: {len(members):,} attachments ({where}), each replaced by a note where it was attached. {note}",
                )
            out.append(r)
            continue
        _, _, latest = r.reason.partition("; ")
        # Attachments, not copies: one copy can ride on several messages, and
        # each note says "one of N copies" counting distinct copies.
        if len(members) == 1:
            what = "an earlier copy of a recurring report, replaced by a note where it was attached"
        else:
            what = f"{len(members):,} attachments, earlier copies of a recurring report, each replaced by a note where it was attached"
        out.append(
            replace(
                r,
                count=len(members),
                size=r.size if len(members) == 1 else 0,  # the table shows a size only for a single copy
                reason=f"Withheld: {what} ({where}); {latest}",
            )
        )
    return out


def build_exceptions_html(
    bundles: list[MessageBundle],
    plan: Plan | None,
    options: Options,
    render_failures: list[dict] | None = None,
    prepared: datetime | None = None,
) -> str | None:
    """The exceptions table, rendered as front matter behind the index. None when clean."""
    rows = group_withheld(list_exceptions(bundles, plan, options, render_failures))
    if not rows:
        return None
    return render.build_template(
        "exceptions.html",
        request_id=options.request_id,
        prepared=_day(prepared),
        keep_natives=options.keep_natives,
        natives_dir=NATIVES_DIR_NAME,
        bates=options.stamp_bates,
        col_widths=_EXCEPTION_WIDTHS[options.stamp_bates],
        attachment_rows=sum(1 for r in rows if not r.is_message and not r.withheld),
        withheld_rows=sum(r.count for r in rows if r.withheld and r.decision != "omit"),
        withheld_at=decisions.withheld_at(options.withheld_by),
        omitted_rows=sum(r.count for r in rows if r.withheld and r.decision == "omit"),
        message_rows=sum(1 for r in rows if r.is_message),
        rows=[
            {
                "seq": r.seq,
                "bates": r.bates,
                "volume": r.volume,
                "date": _short_date(r.date),
                "filename": r.filename if not r.is_message else "(the message itself)",
                "size": render.human_size(r.size) if not r.is_message and r.count == 1 else "",
                "type": describe_type(r.filename, r.content_type) if not r.is_message else "Message",
                "reason": r.reason[0].upper() + r.reason[1:],
            }
            for r in rows
        ],
    )


def render_exceptions(
    bundles: list[MessageBundle],
    plan: Plan | None,
    options: Options,
    out: Path,
    render_failures: list[dict] | None = None,
    prepared: datetime | None = None,
) -> Path | None:
    """Render the exceptions table to *out*; None when there is nothing to list."""
    html = build_exceptions_html(bundles, plan, options, render_failures, prepared)
    if html is None:
        return None
    out = Path(out)
    render.render_checked(html, out)
    return out


# --- manifest ---------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def sha256_file(path: Path | None) -> str | None:
    if path is None or not Path(path).is_file():
        return None
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_sha256_file = sha256_file


def write_manifest(
    output_dir: Path,
    options: Options,
    stats: Any,
    plan: Plan | None,
    volumes: list[Volume],
    bundles: list[MessageBundle] | None = None,
    appendix_volumes: list[Volume] | None = None,
    native_files: list[NativeFile] | None = None,
) -> Path:
    """manifest.json for the operator: counts, per-volume hashes and ranges,
    the exception list, and the options used. Pass *bundles* to derive the
    exception list; otherwise ``stats.exceptions`` is used as given.

    With ``--appendix-over``, *appendix_volumes* (the written appendix
    volumes) are listed like the message volumes, and every appendix item
    with its range and the messages that carried it (derived from *bundles*).

    Pre-mark coverage appears as per-category counts plus the sha256 of the
    terms file. Neither the terms nor the file's path is written.

    With ``--search-report``, a ``search_report`` block holds the query and
    the export's names and times as Purview recorded them, and the sha256 of
    the Summary CSV (or text file) read. The submitter's address is not kept.

    With ``--triage``, *native_files* are listed with their IDs, file names,
    hashes and the messages that carried them.
    """
    st = _as_stats(stats)
    overall = _overall_range(plan)
    if bundles is not None:
        exceptions = [
            {
                "message": r.seq,
                "attachment": r.index or None,
                "filename": r.filename or None,
                "size": r.size if not r.is_message else None,
                "sha256": r.sha256 or None,
                "content_type": r.content_type or None,
                "reason": r.reason,
                "decision": r.decision or None,
                "bates": r.bates,
                "volume": r.volume,
            }
            for r in list_exceptions(bundles, plan, options, st.render_failures)
        ]
    else:
        exceptions = list(st.exceptions or [])

    counts = {
        f.name: getattr(st, f.name)
        for f in fields(RunStats)
        if f.name not in {"started", "finished", "coverage", "coverage_summary", "terms_file_sha256", "render_failures", "exceptions", "page_budget", "decisions"}
    }
    coverage = st.coverage_summary if st.coverage_summary is not None else summarize_coverage(st.coverage)
    if coverage is not None:
        coverage = dict(coverage)
        coverage["terms_file_sha256"] = st.terms_file_sha256 or sha256_file(options.terms_file)
    opts = _jsonable(options)
    opts["terms_file"] = bool(options.terms_file)
    opts["decisions_file"] = bool(options.decisions_file)
    opts["search_report"] = bool(options.search_report)  # the report itself goes in its own block
    doc = {
        "pst2pdf_version": __version__,
        "manifest_version": 2,
        "request_id": options.request_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "started": st.started.isoformat() if st.started else None,
        "finished": st.finished.isoformat() if st.finished else None,
        "counts": counts,
        "bates": {
            "stamped": options.stamp_bates,  # false: planned but never printed on the page
            "prefix": options.bates.prefix,
            "start": options.bates.start,
            "width": options.bates.width,
            "first": options.bates.label(overall.start) if overall else None,
            "last": options.bates.label(overall.end) if overall else None,
            "pages": (overall.end - overall.start + 1) if overall else 0,
        },
        "volumes": [
            {
                "number": v.number,
                "total": v.total,
                "filename": Path(v.path).name,
                "sha256": sha256_file(Path(v.path)),
                "pages": v.page_count,
                "bates_start": options.bates.label(v.bates.start),
                "bates_end": options.bates.label(v.bates.end),
                "first_message": v.first_seq,
                "last_message": v.last_seq,
            }
            for v in volumes
        ],
        "exceptions": exceptions,
        "coverage": coverage,
        "options": opts,
    }
    if options.input_format == "mbox":
        doc["input"] = {"format": "mbox", "export_source_user_supplied": options.export_source,
                        "search_query_user_supplied": options.search_query,
                        "export_description_user_supplied": options.export_description,
                        "display_timezone": options.display_timezone or "system local",
                        "completeness": "incomplete" if stats.get("incomplete") else "archives read; export completeness not established",
                        "archives": stats.get("intake_archives", []), "archive_errors": stats.get("intake_errors", [])}
        doc["messages"] = [{"message": b.seq, "message_id": b.model.message_id,
                            "in_reply_to": b.model.in_reply_to, "references": b.model.references,
                            "fingerprint": b.model.identity_fingerprint,
                            "original_date": b.model.original_date, "original_timestamp": b.model.original_timestamp,
                            "labels": b.model.gmail_labels, "sources": b.model.source_associations,
                            "intake_issues": b.model.intake_issues} for b in bundles or []]
    if st.decisions is not None:
        doc["decisions"] = st.decisions
    if options.search_report is not None:
        doc["search_report"] = searchreport.manifest_entry(options.search_report)
    if options.appendix_over is not None:
        doc["appendix_volumes"] = [
            {
                "number": v.number,
                "total": v.total,
                "filename": Path(v.path).name,
                "sha256": sha256_file(Path(v.path)),
                "pages": v.page_count,
                "bates_start": options.bates.label(v.bates.start),
                "bates_end": options.bates.label(v.bates.end),
                "first_item": v.first_seq,
                "last_item": v.last_seq,
            }
            for v in appendix_volumes or []
        ]
        doc["appendix_items"] = [
            {
                "item": row["item"],
                "filename": row["filename"],
                "size": row["size"],
                "sha256": row["sha256"],
                "pages": row["pages"],
                "volume": row["volume"],
                "bates_start": options.bates.label(row["range"].start) if row["range"] else None,
                "bates_end": options.bates.label(row["range"].end) if row["range"] else None,
                "messages": row["messages"],
            }
            for row in appendix_rows(bundles or [], plan, options)
        ]
    if options.triage:
        doc["native_files"] = [
            {
                "id": f.id,
                "file": f.file_name,
                "filename": f.filename,
                "sha256": f.sha256,
                "size": f.size,
                "kind": f.kind or None,
                "status": f.status,
                "messages": sorted({seq for seq, _ in f.carriers}),
            }
            for f in native_files or []
        ]
    elif st.page_budget is not None:
        doc["largest_attachments"] = survey.manifest_rows(st.page_budget)
        if options.survey:
            doc["survey"] = survey.manifest_section(st.page_budget)
    out = Path(output_dir) / MANIFEST_NAME
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return out


def write_intake_report(output_dir, options, stats, entries):
    """Internal source traceability, including survey runs with no bundles."""
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = {"format": "mbox", "incomplete": stats.get("incomplete", False),
           "export_completeness": "not established", "archives": stats.get("intake_archives", []),
           "archive_errors": stats.get("intake_errors", []),
           "messages": [{"message": seq, "sources": e.model.source_associations,
                         "labels": e.model.gmail_labels, "original_date": e.model.original_date,
                         "original_timestamp": e.model.original_timestamp,
                         "fingerprint": e.model.identity_fingerprint,
                         "message_id": e.model.message_id, "in_reply_to": e.model.in_reply_to,
                         "references": e.model.references, "issues": e.model.intake_issues}
                        for seq, e in enumerate(entries, 1)]}
    (output_dir / "Intake report - DO NOT RELEASE.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if doc["incomplete"]:
        (output_dir / "INCOMPLETE - DO NOT RELEASE.txt").write_text(
            "INCOMPLETE RUN: one or more source archives could not be fully read. "
            "See Intake report - DO NOT RELEASE.json. Do not release this partial output.\n")
