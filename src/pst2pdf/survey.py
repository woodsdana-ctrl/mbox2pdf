"""The page budget: where a run's pages go, and which attachments fill them.

One aggregation, :func:`measure`, turns the bundles (and the plan, when
there is one) into a :class:`~pst2pdf.model.PageBudget`. Two things read it:

* every normal run, for START HERE's "Largest attachments" section and the
  manifest's ``largest_attachments``;
* ``--survey``, which stops after conversion and writes ``Attachment
  survey.pdf`` and ``Attachment survey.csv`` instead of volumes.

The survey names attachments, so like START HERE it is kept back and never
released. It never carries a pre-marked term: a survey run does not pre-mark.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from . import render, searchreport
from .model import AttachmentSize, MessageBundle, Options, PageBudget, Plan, TypeTotal, WhatIf

SURVEY_PDF_NAME = "Attachment survey.pdf"
SURVEY_CSV_NAME = "Attachment survey.csv"

# Cut-offs for the survey's "what if" table, in pages.
WHAT_IF_THRESHOLDS = (10, 25, 50, 100, 250, 500)
START_HERE_TOP = 10  # rows in START HERE's "Largest attachments"
REPORT_TOP = 25  # rows in the survey PDF and the manifest

# ConvertedAttachment.method (or status, for the two kinds with no pages) ->
# the plain word the reviewer sees in the Type column.
TYPE_LABELS = {
    "text": "Plain text",
    "html": "Web page",
    "libreoffice": "Office file",
    "pdf": "PDF",
    "image": "Picture",
    "message": "Email",
    "calendar": "Calendar item",
    "placeholder": "Not converted",
    "withheld": "Withheld by decision",
    "container": "Zip archive",
    "native": "Separate file",
}

CSV_COLUMNS = (
    "rank",
    "filename",
    "type",
    "method",
    "pages",
    "copies",
    "pages_in_volumes",
    "share_percent",
    "first_message",
    "first_message_date",
    "size_bytes",
    "sha256",
    "status",
    "reason",
    # The reviewers fill these in and pass the file back with --decisions.
    "decision",
    "note",
    "outcome",
)


# --- aggregation ------------------------------------------------------------


def _kind(status: str, method: str) -> str:
    """The key an attachment is grouped under: its method, or its status when it has no pages."""
    return status if status in ("placeholder", "withheld", "container", "native") else method


def type_label(status: str, method: str) -> str:
    kind = _kind(status, method)
    return TYPE_LABELS.get(kind, kind or "Other")


def _content_pages(entry) -> int:
    """Pages of a slot's own converted content (0 for a note of any kind)."""
    conv = entry.converted
    if entry.duplicate_of is not None or conv is None:
        return 0
    if conv.status in ("placeholder", "container") or conv.pdf_path is None:
        return 0
    return max(0, conv.page_count)


def what_if(
    attachments: list[AttachmentSize],
    total_pages: int,
    volume_pages: int,
    thresholds: tuple[int, ...] = WHAT_IF_THRESHOLDS,
) -> list[WhatIf]:
    """For each cut-off, what moving every attachment longer than it out of
    the message volumes would do. Each copy that showed pages in full leaves a
    one-page note behind (later deduped copies already are notes)."""
    rows = []
    limit = max(1, volume_pages)
    for threshold in thresholds:
        moved = [a for a in attachments if a.pages > threshold]
        pages_moved = sum(a.pages_in_volumes for a in moved)
        notes = sum(a.full_copies for a in moved)
        left = total_pages - pages_moved + notes
        rows.append(
            WhatIf(
                threshold=threshold,
                attachments=len(moved),
                pages_moved=pages_moved,
                notes_added=notes,
                pages_left=left,
                volumes_left=math.ceil(left / limit) if left > 0 else 0,
            )
        )
    return rows


def measure(
    bundles: list[MessageBundle],
    options: Options,
    plan: Plan | None = None,
    front_matter_pages: int = 0,
    render_failures: int = 0,
) -> PageBudget:
    """Where the pages go. With a *plan*, every slot's page count and the
    totals come from it, so they match the volumes exactly; without one,
    each note counts one page and *front_matter_pages* stands in for the
    index."""
    bundles = sorted(bundles, key=lambda b: b.seq)
    sizes: dict[str, AttachmentSize] = {}
    slots = 0
    content_total = 0
    note_total = 0
    exceptions = render_failures + sum(bool(b.model.intake_issues) for b in bundles)
    appendix_seen: set[int] = set()  # --appendix-over items already counted in full
    for bundle in bundles:
        for entry in bundle.attachments:
            slots += 1
            in_appendix = entry.appendix_item is not None
            # An appendix item's pages are counted once, at its first carrier;
            # every carrying slot is a one-page pointer in the message volumes.
            full = entry.duplicate_of is None and not (in_appendix and entry.appendix_item in appendix_seen)
            content = _content_pages(entry) if full else 0
            rng = plan.attachment_ranges.get((bundle.seq, entry.index)) if plan else None
            if in_appendix:
                pointer = (rng.end - rng.start + 1) if rng else 1
                header = 1 if full else 0  # the item's header sheet in the appendix
                appendix_seen.add(entry.appendix_item)
                slot_notes = pointer + header
            else:
                slot_pages = (rng.end - rng.start + 1) if rng else (content or 1)
                slot_notes = slot_pages - content
            content_total += content
            note_total += slot_notes
            conv = entry.converted
            if (
                options.convert_attachments
                and entry.duplicate_of is None
                and conv is not None
                and conv.status == "placeholder"
            ):
                exceptions += 1
            size = sizes.get(entry.sha256)
            if size is None:
                # The first slot is never a duplicate, so it carries the conversion.
                size = sizes[entry.sha256] = AttachmentSize(
                    sha256=entry.sha256,
                    filename=entry.filename,
                    content_type=entry.content_type,
                    size=entry.size,
                    status=conv.status if conv else "",
                    method=conv.method if conv else "none",
                    reason=(conv.reason or "").strip() if conv and conv.status in ("placeholder", "withheld") else "",
                    pages=content,
                    copies=0,
                    full_copies=0,
                    first_seq=bundle.seq,
                    first_date=bundle.model.date,
                )
            size.copies += 1
            if full:
                size.full_copies += 1

    ranked = sorted(sizes.values(), key=lambda a: (-a.pages_in_volumes, -a.pages, a.first_seq, a.filename.lower()))
    message_pages = sum(b.message_pages for b in bundles)
    appendix_pages = appendix_volumes = 0
    if plan is not None:
        front = (plan.front_matter.end - plan.front_matter.start + 1) if plan.front_matter else 0
        appendix_pages = sum(v.page_count for v in plan.appendix_volumes)
        appendix_volumes = len(plan.appendix_volumes)
        total = sum(v.page_count for v in plan.volumes) + appendix_pages
        volumes = len(plan.volumes)
    else:
        front = front_matter_pages
        total = front + message_pages + content_total + note_total
        volumes = math.ceil(total / max(1, options.volume_pages)) if total else 0

    groups: dict[str, TypeTotal] = {}
    for a in ranked:
        kind = _kind(a.status, a.method)
        group = groups.setdefault(kind, TypeTotal(kind, type_label(a.status, a.method), 0, 0, 0))
        group.attachments += 1
        group.copies += a.copies
        group.pages += a.pages_in_volumes
    by_type = sorted(groups.values(), key=lambda g: (-g.pages, -g.attachments, g.label))

    return PageBudget(
        messages=len(bundles),
        message_pages=message_pages,
        attachment_slots=slots,
        distinct_attachments=len(sizes),
        attachment_pages=content_total,
        note_pages=note_total,
        front_matter_pages=front,
        total_pages=total,
        volumes=volumes,
        volume_pages=options.volume_pages,
        exceptions=exceptions,
        attachments=ranked,
        by_type=by_type,
        # With the appendix already on, "pages left in the message volumes"
        # has no single baseline; a survey without --appendix-over compares cut-offs.
        what_if=what_if(ranked, total, options.volume_pages) if options.appendix_over is None else [],
        appendix_over=options.appendix_over,
        appendix_items=len(appendix_seen),
        appendix_pages=appendix_pages,
        appendix_volumes=appendix_volumes,
    )


def largest(budget: PageBudget | None, n: int) -> list[AttachmentSize]:
    """The *n* attachments that put the most pages in the volumes (pages only)."""
    if budget is None:
        return []
    return [a for a in budget.attachments if a.pages_in_volumes > 0][:n]


# --- wording ----------------------------------------------------------------


def share(pages: int, total: int) -> float:
    return 100.0 * pages / total if total else 0.0


def share_text(pages: int, total: int) -> str:
    """'61%', '4.2%', '<0.1%'."""
    pct = share(pages, total)
    if pct == 0:
        return "0%"
    if pct < 0.1:
        return "<0.1%"
    if pct < 9.95:
        return f"{pct:.1f}%"
    return f"{pct:.0f}%"


def _short_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "(undated)"


def _day(dt: datetime | None) -> str:
    dt = dt or datetime.now()
    return f"{dt:%B} {dt.day}, {dt:%Y}"


def _row(rank: int, a: AttachmentSize, total: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "filename": a.filename or "(no name)",
        "type": type_label(a.status, a.method),
        "pages": a.pages,
        "copies": a.copies,
        "pages_in_volumes": a.pages_in_volumes,
        "share": share_text(a.pages_in_volumes, total),
        "first_seq": a.first_seq,
        "first_date": _short_date(a.first_date),
    }


def _top_context(budget: PageBudget, n: int) -> dict[str, Any]:
    top = largest(budget, n)
    top_pages = sum(a.pages_in_volumes for a in top)
    with_pages = sum(1 for a in budget.attachments if a.pages_in_volumes > 0)
    return {
        "rows": [_row(i, a, budget.total_pages) for i, a in enumerate(top, start=1)],
        "count": len(top),
        "with_pages": with_pages,
        "top_pages": top_pages,
        "top_share": share_text(top_pages, budget.total_pages),
        "attachment_pages": budget.attachment_pages,
        "attachment_share": share_text(budget.attachment_pages, budget.total_pages),
        "total_pages": budget.total_pages,
    }


def start_here_context(budget: PageBudget | None, n: int = START_HERE_TOP) -> dict[str, Any] | None:
    """START HERE's "Largest attachments" section, or None when no attachment has pages."""
    if budget is None or not largest(budget, 1):
        return None
    return _top_context(budget, n)


def manifest_rows(budget: PageBudget | None, n: int = REPORT_TOP) -> list[dict[str, Any]]:
    """``largest_attachments`` in manifest.json: the top *n* with their sha256."""
    if budget is None:
        return []
    return [
        {
            "rank": i,
            "filename": a.filename,
            "type": type_label(a.status, a.method),
            "method": a.method,
            "pages": a.pages,
            "copies": a.copies,
            "pages_in_volumes": a.pages_in_volumes,
            "share_percent": round(share(a.pages_in_volumes, budget.total_pages), 2),
            "first_message": a.first_seq,
            "sha256": a.sha256,
        }
        for i, a in enumerate(largest(budget, n), start=1)
    ]


def manifest_section(budget: PageBudget) -> dict[str, Any]:
    """The ``survey`` block of a survey run's manifest: the projection in numbers."""
    return {
        "note": "survey run: no volumes were written, so every page count here is a projection",
        "projected_pages": budget.total_pages,
        "projected_volumes": budget.volumes,
        "volume_pages": budget.volume_pages,
        "messages": budget.messages,
        "message_pages": budget.message_pages,
        "attachment_slots": budget.attachment_slots,
        "distinct_attachments": budget.distinct_attachments,
        "attachment_pages": budget.attachment_pages,
        "note_pages": budget.note_pages,
        "front_matter_pages": budget.front_matter_pages,
        "exceptions": budget.exceptions,
        "by_type": [
            {"type": g.label, "method": g.method, "attachments": g.attachments, "copies": g.copies, "pages": g.pages}
            for g in budget.by_type
        ],
        "what_if": [
            {
                "longer_than": w.threshold,
                "attachments": w.attachments,
                "pages_moved": w.pages_moved,
                "notes_added": w.notes_added,
                "pages_left": w.pages_left,
                "volumes_left": w.volumes_left,
            }
            for w in budget.what_if
        ],
    }


# --- CSV --------------------------------------------------------------------


def excel_cell(value: Any) -> Any:
    """Attachment names come from strangers. A cell Excel would read as a
    formula (=, +, -, @, or a leading tab/return) is prefixed with a quote."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def build_csv(budget: PageBudget, rules=None, outcome=None) -> str:
    """Every distinct attachment, most pages first, one row each, with empty
    decision and note columns for the reviewers. After a run with ``--decisions`` the
    CSV carries them forward: a sha256 row's decision stays on its row, rows
    added by hand (a filename or a pattern) follow at the end, and the
    outcome column says what each decision did."""
    rules = list(rules or [])
    by_sha = {r.sha256: r for r in rules if r.sha256}
    results = outcome.results() if outcome is not None else {}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for rank, a in enumerate(budget.attachments, start=1):
        rule = by_sha.get(a.sha256)
        writer.writerow(
            [
                excel_cell(v)
                for v in (
                    rank,
                    a.filename,
                    type_label(a.status, a.method),
                    a.method,
                    a.pages,
                    a.copies,
                    a.pages_in_volumes,
                    f"{share(a.pages_in_volumes, budget.total_pages):.2f}",
                    a.first_seq,
                    a.first_date.isoformat() if a.first_date else "",
                    a.size,
                    a.sha256,
                    a.status,
                    " ".join(a.reason.split()),
                    rule.decision if rule else "",
                    rule.note if rule else "",
                    results.get(a.sha256, ""),
                )
            ]
        )
    for rule in rules:
        if not rule.sha256:
            row = {"filename": rule.filename, "decision": rule.decision, "note": rule.note}
            writer.writerow([excel_cell(row.get(col, "")) for col in CSV_COLUMNS])
    return buf.getvalue()


def write_excel_csv(path: Path, text: str) -> Path:
    """UTF-8 with a byte-order mark (and the CRLF the builders use), so Excel
    on Windows opens it cleanly."""
    path = Path(path)
    path.write_bytes(("\ufeff" + text).encode("utf-8"))
    return path


def write_csv(budget: PageBudget, output_dir: Path, rules=None, outcome=None) -> Path:
    return write_excel_csv(Path(output_dir) / SURVEY_CSV_NAME, build_csv(budget, rules, outcome))


# --- the survey PDF ---------------------------------------------------------


def _settings(options: Options) -> list[str]:
    return [
        "identical attachments shown in full once" if options.dedup_attachments else "identical attachments shown in full every time",
        "attachments converted to pages" if options.convert_attachments else "attachments not converted (one note each)",
        "zip files opened" if options.expand_zips else "zip files listed, not opened",
        "duplicate messages shown once" if options.dedup_messages else "every copy of a duplicated message kept",
        f"volumes of up to {options.volume_pages:,} pages",
    ]


def build_survey_html(options: Options, budget: PageBudget, stats: Any = None) -> str:
    """The survey PDF's HTML. *stats* is the run's stats mapping (or RunStats)."""

    def get(name: str, default: Any = 0) -> Any:
        if stats is None:
            return default
        if isinstance(stats, dict):
            return stats.get(name, default)
        return getattr(stats, name, default)

    return render.build_template(
        "survey.html",
        request_id=options.request_id,
        prepared=_day(get("started", None)),
        budget=budget,
        top=_top_context(budget, REPORT_TOP),
        by_type=[
            {
                "label": g.label,
                "attachments": g.attachments,
                "copies": g.copies,
                "pages": g.pages,
                "share": share_text(g.pages, budget.total_pages),
            }
            for g in budget.by_type
        ],
        what_if=budget.what_if,
        messages_found=get("messages_found", budget.messages),
        duplicates_removed=get("duplicates_removed", 0),
        render_failures=len(get("render_failures", []) or []),
        intake_exceptions=get("intake_exceptions", 0),
        attachment_exceptions=budget.exceptions - len(get("render_failures", []) or []) - get("intake_exceptions", 0),
        dedup_attachments=options.dedup_attachments,
        convert_attachments=options.convert_attachments,
        settings=_settings(options),
        csv_name=SURVEY_CSV_NAME,
        search=searchreport.context(options.search_report),
        options=options,
    )


def write_survey_pdf(output_dir: Path, options: Options, budget: PageBudget, stats: Any = None) -> Path:
    out = Path(output_dir) / SURVEY_PDF_NAME
    render.render_checked(build_survey_html(options, budget, stats), out)
    return out
