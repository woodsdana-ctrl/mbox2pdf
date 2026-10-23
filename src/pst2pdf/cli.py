"""Command-line entry point: pst2pdf INPUT.pst OUTPUT_DIR.

Every run extracts, parses and organizes the archive, then
``build_volumes`` writes review volumes (Bates-numbered unless
``--no-bates`` is given), or with ``--survey`` only counts their pages.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ALL_COMPLETED, FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from . import __version__, archives, assemble, decisions, extract, natives, organize, parse, render, report, searchreport, survey, wordlist
from . import calendar as icscal
from .model import (
    AppendixItem,
    AttachmentEntry,
    BatesSpec,
    ConvertedAttachment,
    MessageBundle,
    MessageModel,
    NativeFile,
    Options,
    PageRange,
)

log = logging.getLogger(__name__)

WORDLIST_NAME = "Redaction word list.txt"
PLAN_ROUNDS = 3
SEPARATE_REMOVED = (
    "--separate was removed: the one-PDF-per-message layout is gone. "
    "For attachments as separate files, use --triage."
)


def pdf_page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def stage(msg: str) -> None:
    """Progress line the wrapper script shows as a step."""
    log.info("stage: %s", msg)


class _SerialExecutor:
    """Drop-in for ProcessPoolExecutor that runs each job inline (``--jobs 1``).

    Keeps the orchestration loop on a single code path, avoids spawning a
    worker just to do one thing at a time, and lets tests exercise the loop
    without WeasyPrint or child processes.
    """

    def submit(self, fn, *args) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - mirrored into the future
            fut.set_exception(exc)
        return fut

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def default_jobs() -> int:
    return os.cpu_count() or 1


def make_executor(jobs: int):
    return _SerialExecutor() if jobs == 1 else ProcessPoolExecutor(max_workers=jobs)


def parse_tree(extract_root: Path, stats: dict) -> list[MessageModel]:
    """Parse every mail and calendar item under *extract_root* in sorted order.

    Every timezone-aware date is converted to the container's local zone
    (``TZ`` is passed in by the wrapper), so the pages show one clock instead
    of whatever offset each sender's mail client wrote. Naive dates (all-day
    calendar items) are left alone.
    """
    models: list[MessageModel] = []
    # readpst wraps everything in one directory named after the archive; that
    # name means nothing to the reader, so folder paths start below it.
    children = [c for c in extract_root.iterdir() if c.is_dir()]
    base = children[0] if len(children) == 1 and not any(c.is_file() for c in extract_root.iterdir()) else extract_root
    for path in sorted(extract_root.rglob("*")):
        if path.is_dir():
            continue
        folder = path.parent.relative_to(base).as_posix() if path.is_relative_to(base) else path.parent.relative_to(extract_root).as_posix()
        folder = "" if folder == "." else folder
        suffix = path.suffix.lower()
        try:
            if suffix == ".eml":
                models.append(parse.parse_eml(path, folder=folder))
            elif suffix == ".ics":
                models.append(icscal.parse_ics(path, folder=folder))
                stats["calendar_items"] += 1
            else:
                log.debug("skipping non-mail item: %s", path)
                stats["skipped_non_mail"] += 1
        except Exception as exc:
            log.error("failed to parse %s: %s", path, exc)
            stats["errors"] += 1
    for model in models:
        if model.date is not None and model.date.tzinfo is not None:
            model.date = model.date.astimezone()
    return models


# ---------------------------------------------------------------------------
# Review volumes (default)
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    """A render job waiting on the pool: which entry, which output."""

    seq: int
    out: Path


def _header_origin(entry: organize.Organized, seq: int, options: Options) -> dict:
    """What a message header says about where the message sits in the archive:
    its number, its folders and how many copies were found. A released page
    carries none of it, because a message number or a count of copies tells
    the requester how much was collected and reviewed, and a folder name can
    be anything the mailbox owner typed. The triage copy is never released and
    its attachment index names carrying messages by number, so it keeps them."""
    if options.triage:
        return {"seq": seq, "folders": entry.folders, "copies": entry.copies}
    return {"seq": None, "folders": [], "copies": 1}


def _render_fallback(
    entry: organize.Organized, seq: int, out: Path, options: Options, stats: dict, offpage: bool = False
) -> bool:
    """Render a message whose real page failed: first as plain text under the
    same header, then as the header alone. A message never vanishes from the
    volumes because its HTML was odd. Returns False only if even the header
    page could not be written, which is counted as an error.

    *offpage* is for a page that rendered but left text outside the page:
    the plain-text page then says so, and it is counted apart from failures."""
    for level in ("offpage" if offpage else "text", "header"):
        html = render.fallback_html(entry.model, level, **_header_origin(entry, seq, options))
        try:
            result = render.render_pdf(html, out)
        except Exception as exc:
            log.error("fallback (%s) for message %d also failed: %s", level, seq, exc)
            continue
        stats["blocked_urls"] += result.blocked
        render.note_offpage(result, f"message {seq}")
        if offpage and level != "header":
            stats["offpage_fallbacks"] += 1
            log.warning("message %d is shown as unformatted text: its layout put text outside the page", seq)
            return True
        stats["render_fallbacks"] += 1
        if level == "header":
            stats["render_failures"].append({"message": seq, "reason": "the message body could not be rendered"})
            log.warning("message %d is shown as its header only", seq)
        else:
            log.warning("message %d is shown as unformatted text", seq)
        return True
    return False


def _render_messages(
    entries: list[organize.Organized], work: Path, jobs: int, options: Options, stats: dict
) -> dict[int, Path]:
    """Render every message page through the pool; returns seq -> PDF path."""
    out_dir = work / "messages"
    out_dir.mkdir(parents=True, exist_ok=True)
    done: dict[int, Path] = {}
    pending: dict[Future, _Pending] = {}
    max_pending = jobs * 2

    def finish(fut: Future) -> None:
        job = pending.pop(fut)
        try:
            result = fut.result()
        except Exception as exc:
            log.error("failed to render message %d: %s", job.seq, exc)
            # Fallbacks are rare, so they render inline in the parent.
            if not _render_fallback(entries[job.seq - 1], job.seq, job.out, options, stats):
                stats["errors"] += 1
                return
        else:
            stats["blocked_urls"] += result.blocked
            if result.offpage:
                # Text past the page edge is invisible to the reviewer but
                # still in the text layer: never let such a page through.
                if not _render_fallback(entries[job.seq - 1], job.seq, job.out, options, stats, offpage=True):
                    stats["errors"] += 1
                    return
            log.info("wrote message %d", job.seq)
        done[job.seq] = job.out

    with make_executor(jobs) as pool:
        for seq, entry in enumerate(entries, start=1):
            html = render.build_html(entry.model, **_header_origin(entry, seq, options))
            out = out_dir / f"{seq:05d}.pdf"
            fut = pool.submit(render.render_pdf, html, out)
            pending[fut] = _Pending(seq, out)
            if len(pending) >= max_pending:
                for f in wait(pending, return_when=FIRST_COMPLETED)[0]:
                    finish(f)
        for f in wait(pending, return_when=ALL_COMPLETED)[0]:
            finish(f)
    return done


def _apply_decisions(entries, options: Options, stats: dict) -> dict[str, decisions.Withheld | decisions.Omitted]:
    """The reviewers' decisions (--decisions): which attachment bytes to withhold.
    Records every rule and what it matched for the manifest, and warns about
    a rule that matched nothing (a typo in a pattern would otherwise withhold
    less than the reviewers asked for, silently)."""
    if not options.decisions_file:
        return {}
    rules = decisions.load(options.decisions_file)
    outcome = decisions.apply(entries, rules)
    stats["decision_rules"] = rules
    stats["decision_outcome"] = outcome
    for rule in outcome.unmatched(rules):
        log.warning("decisions row %d (%s) matched no attachment", rule.line, rule.filename or rule.sha256)
    stats["decisions"] = {
        "file_sha256": report.sha256_file(options.decisions_file),
        "rules": [
            {
                "line": r.line,
                "decision": r.decision,
                "filename": r.filename or None,
                "sha256": r.sha256 or None,
                "note": r.note or None,
                "attachments_matched": outcome.matched.get(r.line, 0),
                "kept_sha256": outcome.kept.get(r.line, []),
                "withheld_sha256": sorted(s for s, w in outcome.withheld.items() if w.rule.line == r.line),
            }
            for r in rules
        ],
    }
    return outcome.withheld


def _convert_attachments(
    entries,
    options: Options,
    work: Path,
    withheld: dict[str, decisions.Withheld | decisions.Omitted] | None = None,
    stats: dict | None = None,
) -> dict[str, ConvertedAttachment]:
    """Convert every distinct attachment once; returns sha256 -> result.
    Bytes a decision withholds are never converted. Remote resources
    refused while laying out HTML attachments are added to
    ``stats["blocked_urls"]``, as the message pages' are."""
    results: dict[str, ConvertedAttachment] = {}
    for entry in entries:
        for att in entry.model.attachments:
            why = (withheld or {}).get(att.sha256)
            if why is not None:
                results.setdefault(
                    att.sha256,
                    ConvertedAttachment(att, "withheld", "none", None, 0, why.reason(options.withheld_by), why.summary(), why.rule.decision),
                )
    if options.triage:
        # Nothing is converted: each attachment is written as the original file.
        for entry in entries:
            for att in entry.model.attachments:
                results.setdefault(att.sha256, ConvertedAttachment(attachment=att, status="native", method="none", pdf_path=None))
        return results
    if not options.convert_attachments:
        for entry in entries:
            for att in entry.model.attachments:
                results.setdefault(
                    att.sha256,
                    ConvertedAttachment(
                        attachment=att,
                        status="placeholder",
                        method="none",
                        pdf_path=None,
                        reason="attachment conversion was turned off for this run",
                    ),
                )
        return results

    from . import convert  # heavy imports (Pillow, pypdf) stay out of runs that convert nothing

    with convert.Converter(work, options) as converter:
        futures = {}
        for entry in entries:
            for att in entry.model.attachments:
                if att.sha256 not in futures and att.sha256 not in results:
                    futures[att.sha256] = converter.submit(att)
        for digest, fut in futures.items():
            results[digest] = fut.result()
        if stats is not None:
            stats["blocked_urls"] += converter.blocked_urls
    return results


def _expand_zips(entries, options: Options, spool_dir: Path | None = None) -> dict[int, dict[str, str]]:
    """Expand zip attachments in place. Returns, per seq, the zip filenames
    that gained entries mapped to the notes about what was skipped inside."""
    containers: dict[int, dict[str, str]] = {}
    for seq, entry in enumerate(entries, start=1):
        if not any(archives.looks_like_zip(a) for a in entry.model.attachments):
            continue
        before = [a.filename for a in entry.model.attachments]
        entry.model.attachments, notes = archives.expand_all(entry.model.attachments, options.attachment_max_bytes)
        for zip_name, skipped in notes.items():
            log.warning("%s: %s", zip_name, "; ".join(skipped))
        if spool_dir is not None:
            from .mbox import spool_attachments
            spool_attachments([entry.model], spool_dir)
        after = [a.filename for a in entry.model.attachments]
        for name in before:
            if any(a.startswith(name + "/") for a in after):
                containers.setdefault(seq, {})[name] = "\n".join(notes.get(name, []))
    return containers


def _mark_containers(bundles: list[MessageBundle], containers: dict[int, dict[str, str]]) -> None:
    """A zip that was expanded is a container, not a conversion failure: its
    cover sheet says where its files are, and it is not an exception."""
    for bundle in bundles:
        names = containers.get(bundle.seq)
        if not names:
            continue
        for entry in bundle.attachments:
            if entry.filename in names and entry.converted is not None and entry.duplicate_of is None:
                entry.converted = replace(
                    entry.converted, status="container", pdf_path=None, page_count=0, reason=names[entry.filename]
                )


def _build_bundles(entries, message_pdfs, converted, options: Options) -> list[MessageBundle]:
    first_seen: dict[str, tuple[int, int]] = {}  # sha256 -> (seq, index) of first copy
    bundles: list[MessageBundle] = []
    for seq, entry in enumerate(entries, start=1):
        pdf = message_pdfs.get(seq)
        if pdf is None:
            continue  # even the header-only fallback failed; already counted as an error
        bundle = MessageBundle(
            seq=seq,
            model=entry.model,
            folders=list(entry.folders),
            thread_id=entry.thread_id,
            message_pdf=pdf,
            message_pages=pdf_page_count(pdf),
            copies=entry.copies,
        )
        total = len(entry.model.attachments)
        for index, att in enumerate(entry.model.attachments, start=1):
            digest = att.sha256
            duplicate_of = None
            conv: ConvertedAttachment | None = converted[digest]
            # A withheld copy gets its own note in every message that carried
            # it, not a cross-reference to another message's note. So does a
            # triage slot: its page names the file directly.
            own_page = conv is not None and conv.status in ("withheld", "native")
            if options.dedup_attachments and not own_page and digest in first_seen and first_seen[digest][0] != seq:
                duplicate_of = first_seen[digest][0]
                conv = None
            else:
                first_seen.setdefault(digest, (seq, index))
            bundle.attachments.append(
                AttachmentEntry(
                    index=index,
                    total=total,
                    filename=att.filename,
                    size=att.size,
                    content_type=att.content_type,
                    sha256=digest,
                    converted=conv,
                    duplicate_of=duplicate_of,
                )
            )
        bundles.append(bundle)
    return bundles


def _appendix_eligible(conv: ConvertedAttachment | None, options: Options) -> bool:
    """Does this conversion go to the appendix volumes? Only real pages
    (converted or passed-through PDFs) longer than ``--appendix-over``;
    placeholders and zip-contents sheets always stay with their message."""
    if options.appendix_over is None or conv is None:
        return False
    if conv.status not in ("converted", "passthrough") or conv.pdf_path is None:
        return False
    if conv.page_count <= 0:
        conv.page_count = pdf_page_count(conv.pdf_path)
    return conv.page_count > options.appendix_over


def _assign_appendix(
    bundles: list[MessageBundle], converted: dict[str, ConvertedAttachment], options: Options
) -> list[AppendixItem]:
    """Move long attachments out of the message stream (``--appendix-over``).

    Every slot whose attachment qualifies becomes a one-page pointer
    (``appendix_item`` set, never ``duplicate_of``: duplicates point at the
    appendix, not at the first message). With attachment dedup on, identical
    bytes share one item; with it off, each slot has its own. Items are
    numbered in order of first reference in the message stream. Because the
    rule is decided per sha256, a cross-reference never points at a slot that
    moved to the appendix.
    """
    if options.appendix_over is None:
        return []
    items: list[AppendixItem] = []
    by_key: dict[object, AppendixItem] = {}
    for bundle in sorted(bundles, key=lambda b: b.seq):
        for entry in bundle.attachments:
            conv = converted.get(entry.sha256)
            if not _appendix_eligible(conv, options):
                continue
            key = entry.sha256 if options.dedup_attachments else (bundle.seq, entry.index)
            item = by_key.get(key)
            if item is None:
                item = AppendixItem(
                    number=len(items) + 1,
                    filename=entry.filename,
                    size=entry.size,
                    content_type=entry.content_type,
                    sha256=entry.sha256,
                    converted=conv,
                )
                items.append(item)
                by_key[key] = item
            item.carriers.append((bundle.seq, entry.index))
            entry.converted = conv
            entry.duplicate_of = None
            entry.appendix_item = item.number
    return items


def _render_pages(jobs_html: list[tuple[object, str, Path]], jobs: int) -> list[tuple[object, Path]]:
    """Render (key, html, out) jobs through the pool; failures raise."""
    done = []
    with make_executor(jobs) as pool:
        futures = {pool.submit(render.render_pdf, html, out): (key, out) for key, html, out in jobs_html}
        for fut in wait(futures, return_when=ALL_COMPLETED)[0]:
            render.note_offpage(fut.result(), futures[fut][1].name)
            done.append(futures[fut])
    return done


def _render_appendix_headers(items: list[AppendixItem], bundles: list[MessageBundle], options: Options, work: Path, jobs: int) -> None:
    """The one-page header sheet in front of each appendix item. It names no
    page numbers, so unlike the pointer pages it is rendered before planning."""
    if not items:
        return
    pages = work / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    by_seq = {b.seq: b for b in bundles}
    jobs_html = [
        (item, report.build_appendix_header_html(item, by_seq, options), pages / f"appendix-{item.number:05d}.pdf")
        for item in items
    ]
    for item, out in _render_pages(jobs_html, jobs):
        item.header_pdf = out  # type: ignore[union-attr]


def _render_appendix_pointers(
    bundles: list[MessageBundle], items: list[AppendixItem], plan, options: Options, work: Path, jobs: int
) -> None:
    """The one-page note in each slot whose attachment is in the appendix.
    It quotes the item's appendix volume and range, so it needs the final
    plan; it is always one page, so it cannot change the plan."""
    if not items:
        return
    pages = work / "pages"
    by_number = {i.number: i for i in items}
    jobs_html = []
    for bundle in bundles:
        for entry in bundle.attachments:
            if entry.appendix_item is None:
                continue
            html = report.build_appendix_pointer_html(entry, bundle, by_number[entry.appendix_item], plan, options)
            jobs_html.append((entry, html, pages / f"{bundle.seq:05d}-{entry.index:03d}-appendix.pdf"))
    for entry, out in _render_pages(jobs_html, jobs):
        entry.separator_pdf = out  # type: ignore[union-attr]


def _render_attachment_pages(
    bundles: list[MessageBundle],
    options: Options,
    work: Path,
    jobs: int,
    stats: dict,
    files: list[NativeFile] | None = None,
) -> None:
    """Placeholder and zip-contents pages, rendered through the pool.

    A converted attachment gets no sheet of its own: its pages follow the
    message directly, and the index, the bookmarks and the message header's
    attachment list say what they are. Only an attachment with no pages of
    its own needs a page to stand in for it. Cross-reference pages need Bates
    ranges, so they are rendered later inside the plan loop. In a triage run
    every attachment's page names the file it was written as (*files*).
    """
    by_id = {f.id: f for f in files or []}
    pages = work / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    jobs_html: list[tuple[AttachmentEntry, str, Path]] = []
    for bundle in bundles:
        for entry in bundle.attachments:
            if entry.duplicate_of is not None:
                continue
            status = entry.converted.status if entry.converted is not None else ""
            if status == "placeholder":
                html = report.build_placeholder_html(entry, bundle, options)
            elif status == "withheld":
                html = report.build_withheld_html(entry, bundle, options)
            elif status == "container":
                html = report.build_container_html(entry, bundle, options)
            elif status == "native":
                html = report.build_native_pointer_html(entry, bundle, by_id[entry.file_id], options)
            else:
                continue
            out = pages / f"{bundle.seq:05d}-{entry.index:03d}.pdf"
            jobs_html.append((entry, html, out))

    with make_executor(jobs) as pool:
        futures = {pool.submit(render.render_pdf, html, out): (entry, out) for entry, html, out in jobs_html}
        for fut in wait(futures, return_when=ALL_COMPLETED)[0]:
            entry, out = futures[fut]
            try:
                render.note_offpage(fut.result(), out.name)
            except Exception as exc:
                log.error("failed to render cover page for %s: %s", entry.filename, exc)
                stats["errors"] += 1
                continue
            entry.separator_pdf = out


def _first_copy(bundles_by_seq: dict[int, MessageBundle], entry: AttachmentEntry) -> tuple[MessageBundle, AttachmentEntry]:
    first_bundle = bundles_by_seq[entry.duplicate_of]  # type: ignore[index]
    for cand in first_bundle.attachments:
        if cand.sha256 == entry.sha256 and cand.duplicate_of is None:
            return first_bundle, cand
    raise RuntimeError(f"cross-reference target for {entry.filename} not found in message {entry.duplicate_of}")


def _concat_pdfs(parts: list[Path], out: Path) -> Path:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for part in parts:
        for page in PdfReader(str(part)).pages:
            writer.add_page(page)
    with open(out, "wb") as fh:
        writer.write(fh)
    return out


def _plan_with_front_matter(
    bundles: list[MessageBundle],
    options: Options,
    work: Path,
    stats: dict,
    appendix: list[AppendixItem] | None = None,
    jobs: int = 1,
):
    """Plan, render cross-references and the front matter (index, then the
    exceptions table), and re-plan until the front matter's page count is
    stable (its Bates strings have fixed width, so this converges in a round
    or two). Returns the plan and the front-matter PDF.

    The exceptions table lives here, inside Volume 01, rather than in a file
    of its own: it carries attachment names, which must go through the same
    redaction pass as every other page.

    With *appendix* items the plan also lays out the appendix volumes after
    the message volumes, and once it has settled the appendix pointer pages
    are rendered against it. They are one page each whatever they say, so
    they never feed back into the plan.
    """
    pages = work / "pages"
    by_seq = {b.seq: b for b in bundles}
    prepared = stats.get("started")
    front = 0
    front_pdf: Path | None = None
    plan = None
    for _round in range(PLAN_ROUNDS):
        plan = assemble.plan(bundles, options, front_matter_pages=front, appendix=appendix)
        for bundle in bundles:
            for entry in bundle.attachments:
                if entry.duplicate_of is None:
                    continue
                first_bundle, first_entry = _first_copy(by_seq, entry)
                first_range = plan.attachment_ranges[(first_bundle.seq, first_entry.index)]
                out = pages / f"{bundle.seq:05d}-{entry.index:03d}-xref.pdf"
                report.render_crossref(entry, bundle, first_bundle, first_range, options, out, plan)
                entry.separator_pdf = out
        parts = [report.render_index(bundles, plan, options, work / "index.pdf", prepared)]
        exceptions_pdf = report.render_exceptions(
            bundles, plan, options, work / "exceptions.pdf", stats.get("render_failures"), prepared
        )
        if exceptions_pdf is not None:
            parts.append(exceptions_pdf)
        front_pdf = parts[0] if len(parts) == 1 else _concat_pdfs(parts, work / "front-matter.pdf")
        count = pdf_page_count(front_pdf)
        if count == front:
            break
        front = count
    else:
        raise RuntimeError("front matter page count did not stabilise")
    if appendix:
        _render_appendix_pointers(bundles, appendix, plan, options, work, jobs)
    return plan, front_pdf


def build_volumes(extract_root: Path, output_dir: Path, options: Options, work: Path, *,
                  prepared_models: list[MessageModel] | None = None, intake_stats: dict | None = None) -> dict:
    """The default pipeline: extracted tree -> review volumes in *output_dir*.
    With ``--triage``: triage-copy volumes, the original files, and the index."""
    if options.triage:
        options = triage_options(options)
    jobs = options.jobs or default_jobs()
    stats: dict = defaultdict(int)
    for key in (
        "messages_found", "messages", "duplicates_removed", "calendar_items",
        "messages_with_images", "attachments", "attachments_converted",
        "attachments_crossref", "attachments_exceptions", "attachments_withheld", "attachments_omitted",
        "attachments_containers", "attachments_native",
        "skipped_non_mail", "rtf_fallbacks", "render_fallbacks", "offpage_fallbacks", "offpage_text",
        "blocked_urls", "errors",
    ):
        stats[key] = 0
    render.offpage_count = 0
    stats["render_failures"] = []
    stats["started"] = datetime.now().astimezone()

    stage("Reading messages")
    if prepared_models is not None:
        models = prepared_models
        stats.update(intake_stats or {})
    elif options.input_format == "mbox":
        from . import mbox
        models = mbox.read(extract_root, work / "intake", stats, options.display_timezone)
    else:
        models = parse_tree(extract_root, stats)
        if options.display_timezone:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(options.display_timezone)
            for model in models:
                if model.date and model.date.tzinfo:
                    model.date = model.date.astimezone(zone)
    stats["messages_found"] = len(models)
    stats["rtf_fallbacks"] = sum(1 for m in models if m.had_rtf_body)

    stage("Removing duplicates and ordering by conversation" if options.order == "thread" else "Ordering messages")
    entries = organize.organize(models, options)
    if options.input_format == "mbox":
        from . import mbox
        stats["intake_exceptions"] = sum(bool(entry.model.intake_issues) for entry in entries)
        report.write_intake_report(output_dir, options, stats, entries)
    stats["messages"] = len(entries)
    stats["duplicates_removed"] = len(models) - len(entries)
    containers = _expand_zips(entries, options, work / "intake" / "attachments" if options.input_format == "mbox" else None) if options.expand_zips else {}

    if options.input_format == "mbox":
        mbox.spool_attachments([entry.model for entry in entries], work / "intake" / "attachments")

    stage("Rendering message pages")
    message_pdfs = _render_messages(entries, work, jobs, options, stats)

    withheld = _apply_decisions(entries, options, stats)

    stage("Listing attachments" if options.triage else "Converting attachments")
    converted = _convert_attachments(entries, options, work, withheld, stats)

    bundles = _build_bundles(entries, message_pdfs, converted, options)
    appendix = _assign_appendix(bundles, converted, options)
    _mark_containers(bundles, containers)
    files = natives.assign_ids(bundles) if options.triage else []
    # Same rule as the index's Pict. column (embedded images or an image attachment).
    stats["messages_with_images"] = sum(1 for b in bundles if report.has_picture(b))
    for bundle in bundles:
        for entry in bundle.attachments:
            stats["attachments"] += 1
            status = entry.converted.status if entry.converted is not None else ""
            if entry.appendix_item is not None:
                stats["attachments_appendix"] += 1
            elif entry.duplicate_of is not None:
                stats["attachments_crossref"] += 1
            elif status == "container":
                stats["attachments_containers"] += 1
            elif status == "withheld":
                stats["attachments_omitted" if entry.converted.decision == "omit" else "attachments_withheld"] += 1
            elif status == "native":
                stats["attachments_native"] += 1
            elif status == "placeholder":
                # With conversion turned off every attachment is a placeholder
                # by design, which START HERE explains; it is not a failure.
                if options.convert_attachments:
                    stats["attachments_exceptions"] += 1
            else:
                stats["attachments_converted"] += 1
    if options.survey:
        return _finish_survey(bundles, options, output_dir, work, stats, appendix)

    stats.setdefault("attachments_appendix", 0)  # present even with --appendix-over off
    stats["appendix_items"] = len(appendix)

    stats["native_files"] = sum(1 for f in files if f.file_name)

    stage("Building attachment pages and index")
    _render_attachment_pages(bundles, options, work, jobs, stats, files)
    _render_appendix_headers(appendix, bundles, options, work, jobs)
    plan, front_pdf = _plan_with_front_matter(bundles, options, work, stats, appendix, jobs)
    stats["page_budget"] = survey.measure(bundles, options, plan, render_failures=len(stats["render_failures"]))

    stage(f"Writing {len(plan.volumes)} volume{'s' if len(plan.volumes) != 1 else ''}")
    volumes = assemble.write_volumes(plan, bundles, front_pdf, options, output_dir)
    appendix_volumes: list = []
    if plan.appendix_volumes:
        n = len(plan.appendix_volumes)
        stage(f"Writing {n} attachment volume{'s' if n != 1 else ''}")
        appendix_volumes = assemble.write_appendix(plan, appendix, bundles, options, output_dir)

    if options.terms_file or options.patterns:
        stage("Marking terms for redaction")
        from . import premark

        terms = premark.load_terms(options.terms_file) if options.terms_file else []
        coverage = premark.apply([v.path for v in [*volumes, *appendix_volumes]], terms, options.patterns or None, options)
        stats["coverage"] = coverage
        stats["coverage_summary"] = report.summarize_coverage(coverage, terms)
        stats["terms_file_sha256"] = report.sha256_file(options.terms_file)
        # The per-term detail goes to its own file, never to START HERE or the manifest.
        report.write_premark_detail(output_dir, coverage, terms)

    if options.triage:
        stage("Writing the original files, the attachment index, and the manifest")
        stats["natives_dir"] = str(natives.write_files(files, bundles, output_dir))
        stats["index_csv"] = str(
            natives.write_index_csv(files, bundles, output_dir, stats.get("decision_rules"), stats.get("decision_outcome"))
        )
        stats["native_bytes"] = sum(f.size for f in files if f.file_name)
    else:
        stage("Writing guide, word list, and manifest")
        wordlist.write([e.model for e in entries], output_dir)
        if options.keep_natives:
            natives.write_kept(bundles, output_dir)
    stats["finished"] = datetime.now().astimezone()
    report.write_start_here(output_dir, options, stats, plan, files)
    stats["offpage_text"] = render.offpage_count
    stats["manifest"] = str(
        report.write_manifest(
            output_dir, options, stats, plan, volumes, bundles=bundles, appendix_volumes=appendix_volumes, native_files=files
        )
    )
    stats["volumes"] = len(volumes)
    stats["appendix_volumes"] = len(appendix_volumes)
    if plan.volumes and options.stamp_bates:
        stats["bates_first"] = options.bates.label(plan.volumes[0].bates.start)
        stats["bates_last"] = options.bates.label([*plan.volumes, *plan.appendix_volumes][-1].bates.end)
        stats["bates_volumes_last"] = options.bates.label(plan.volumes[-1].bates.end)
        if plan.appendix_volumes:
            stats["bates_appendix_first"] = options.bates.label(plan.appendix_volumes[0].bates.start)
    stats["volume_pages"] = sum(v.page_count for v in volumes)
    stats["appendix_pages"] = sum(v.page_count for v in appendix_volumes)
    stats["pages"] = stats["volume_pages"] + stats["appendix_pages"]
    return dict(stats)


# ---------------------------------------------------------------------------
# Attachment survey (--survey)
# ---------------------------------------------------------------------------


def _survey_plan(bundles: list[MessageBundle], options: Options, work: Path, stats: dict, appendix: list[AppendixItem] | None = None):
    """The plan a full run would make: the same loop as
    ``_plan_with_front_matter``, with the index and the exceptions table laid
    out for real, but no cross-reference pages rendered, since the plan counts
    each of them as one page whatever it says."""
    prepared = stats.get("started")
    front = 0
    for _round in range(PLAN_ROUNDS):
        plan = assemble.plan(bundles, options, front_matter_pages=front, appendix=appendix)
        parts = [report.render_index(bundles, plan, options, work / "index.pdf", prepared)]
        exceptions_pdf = report.render_exceptions(
            bundles, plan, options, work / "exceptions.pdf", stats.get("render_failures"), prepared
        )
        if exceptions_pdf is not None:
            parts.append(exceptions_pdf)
        count = sum(pdf_page_count(p) for p in parts)
        if count == front:
            return plan
        front = count
    raise RuntimeError("front matter page count did not stabilise")


def _finish_survey(
    bundles: list[MessageBundle], options: Options, output_dir: Path, work: Path, stats: dict,
    appendix: list[AppendixItem] | None = None,
) -> dict:
    """``--survey``: messages are laid out and attachments converted, so every
    page count is real. Plan as a full run would, then write the survey, the
    CSV and the manifest, and stop: no volumes, stamps, word list, pre-marks
    or START HERE."""
    stage("Counting pages")
    plan = _survey_plan(bundles, options, work, stats, appendix)
    budget = survey.measure(bundles, options, plan, render_failures=len(stats["render_failures"]))
    stats["page_budget"] = budget

    stage("Writing the attachment survey")
    stats["finished"] = datetime.now().astimezone()
    output_dir.mkdir(parents=True, exist_ok=True)
    stats["survey_pdf"] = str(survey.write_survey_pdf(output_dir, options, budget, stats))
    stats["survey_csv"] = str(survey.write_csv(budget, output_dir, stats.get("decision_rules"), stats.get("decision_outcome")))
    stats["offpage_text"] = render.offpage_count
    stats["manifest"] = str(report.write_manifest(output_dir, options, stats, None, [], bundles=bundles))
    stats["volumes"] = budget.volumes
    stats["pages"] = budget.total_pages
    return dict(stats)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def triage_options(options: Options) -> Options:
    """What ``--triage`` implies. Nothing is converted and each attachment is
    written once as the original file. The volumes are for reading, not for
    redaction or release, so they are not numbered (the production run's
    numbers would differ) and not pre-marked, and no word list is written."""
    return replace(
        options,
        stamp_bates=False,
        convert_attachments=False,
        keep_natives=False,
        dedup_attachments=True,
        terms_file=None,
        patterns=[],
        appendix_over=None,
    )


def _parse_overlay(values: list[str] | None) -> dict[str, str] | None:
    if not values:
        return None
    overlay = {}
    for item in values:
        key, sep, text = item.partition("=")
        if not sep or not key.strip():
            raise argparse.ArgumentTypeError(f"--overlay expects CATEGORY=TEXT, got {item!r}")
        overlay[key.strip().lower()] = text.strip()
    return overlay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pst2pdf",
        description="Convert MBOX exports or an Outlook .pst archive into Bates-numbered PDF review "
        "volumes ready for redaction in Acrobat.",
    )
    parser.add_argument("pst", type=Path, metavar="INPUT", help="input .pst, .mbox, or directory of .mbox files")
    parser.add_argument("output_dir", type=Path, help="directory for the volumes")
    parser.add_argument("--request-id", default=None, help="request name shown on START HERE and in PDF titles (default: 'Request' and today's date; never the archive's file name, which could be a person's name)")
    parser.add_argument("--search-report", type=Path, default=None, metavar="PATH", help="the Purview export's report (the Reports zip, the folder it unzips to, or its Summary CSV), whose query START HERE shows exactly as Purview recorded it; or a text file holding only the query")
    parser.add_argument("--input-format", choices=["auto", "pst", "mbox"], default="auto", help="input type (default: detect .mbox or directory; otherwise PST)")
    parser.add_argument("--display-timezone", metavar="ZONE", help="IANA display timezone, e.g. America/Chicago; preserve original timestamps internally")
    parser.add_argument("--export-source", choices=["unknown", "takeout", "vault"], default="unknown", help="user-supplied MBOX export origin; never inferred from filenames")
    parser.add_argument("--search-query", default="", help="user-supplied query for internal reports; does not prove export completeness")
    parser.add_argument("--export-description", default="", help="user-supplied export description for internal reports")
    parser.add_argument("--work-dir", type=Path, default=None, help="directory for intermediate files (default: a temp dir, removed afterwards; a given dir is kept)")
    parser.add_argument("-j", "--jobs", type=int, default=None, metavar="N", help="work on N messages in parallel (default: one per CPU, %d here)" % default_jobs())
    parser.add_argument("-v", "--verbose", action="store_true", help="per-message logging")
    parser.add_argument("--version", action="version", version=f"pst2pdf {__version__}")

    layout = parser.add_argument_group("layout")
    layout.add_argument("--order", choices=["thread", "chrono", "folder"], default="thread", help="message order (default: thread)")
    layout.add_argument("--no-bates", dest="stamp_bates", action="store_false", help="leave the pages unnumbered: no Bates stamp in the footer, and no page references in the index, the exceptions table or the cross-reference pages")
    layout.add_argument("--bates-prefix", default="DOC-", help="page number prefix (default: DOC-)")
    layout.add_argument("--bates-start", type=int, default=1, help="first page number (default: 1)")
    layout.add_argument("--volume-pages", type=int, default=1000, help="pages per volume before a new one starts (default: 1000)")
    layout.add_argument("--separate", action="store_true", help=argparse.SUPPRESS)  # removed; kept only to say so
    layout.add_argument("--appendix-over", type=int, default=None, metavar="PAGES", help="move converted attachments longer than PAGES pages out of the message volumes into separate 'Attachments NN of MM.pdf' volumes, each with a one-page note in its place behind the message (0 moves every converted attachment)")
    layout.add_argument("--triage", action="store_true", help=f"deciding which attachments to produce: convert none, write each once as the original file to '{natives.NATIVES_DIR_NAME}/', and list them in '{natives.INDEX_CSV_NAME}' for the reviewers to mark up and pass back with --decisions; the volumes are triage copies, unnumbered and not for redaction")
    layout.add_argument("--survey", action="store_true", help=f"count pages only: lay out messages and convert attachments as a full run would, then write '{survey.SURVEY_PDF_NAME}' and '{survey.SURVEY_CSV_NAME}' (every attachment, largest first) instead of volumes; --terms, --patterns and --keep-natives are ignored")

    content = parser.add_argument_group("content")
    content.add_argument("--no-dedup-messages", dest="dedup_messages", action="store_false", help="keep every copy of a duplicated message")
    content.add_argument("--no-dedup-attachments", dest="dedup_attachments", action="store_false", help="repeat identical attachments in full instead of cross-referencing")
    content.add_argument("--no-convert-attachments", dest="convert_attachments", action="store_false", help="placeholder pages instead of converted attachments")
    content.add_argument("--decisions", type=Path, metavar="FILE", help=f"the reviewers' decisions: a --survey run's '{survey.SURVEY_CSV_NAME}' or a --triage run's '{natives.INDEX_CSV_NAME}', with its decision column filled in ('latest' keeps only the most recent copy of a recurring report and withholds the rest, each with a note; 'omit', which needs a note, leaves the attachment out and prints the note in its place)")
    content.add_argument("--withheld-by", default="", metavar="NAME", help="who withheld the earlier copies a 'latest' decision covers, as their notes and the exceptions table name them: 'withheld at the direction of NAME' (default: 'withheld at the reviewer's direction')")
    content.add_argument("--keep-natives", action="store_true", help=f"also write original attachment files to '{natives.NATIVES_DIR_NAME}/'")
    content.add_argument("--no-expand-zips", dest="expand_zips", action="store_false", help="leave zip attachments unexpanded (a page listing their contents instead)")

    marks = parser.add_argument_group("pre-marked redactions")
    marks.add_argument("--terms", type=Path, default=None, metavar="FILE", help="mark every occurrence of the names/terms in FILE")
    marks.add_argument("--patterns", default="", metavar="LIST", help="comma-separated: phone, ssn, dob, student-id")
    marks.add_argument("--student-id-regex", default=None, metavar="RE", help="regular expression for the student-id pattern")
    marks.add_argument("--overlay", action="append", metavar="CATEGORY=TEXT", help="text placed over redactions of a category (repeatable)")
    return parser


def default_request_id(today: date | None = None) -> str:
    """'Request 2026-09-17'. The archive's file name is deliberately not used:
    it lands in every volume's PDF Title, and a file name can be a person's name."""
    return f"Request {(today or date.today()).isoformat()}"


def options_from_args(args: argparse.Namespace) -> Options:
    return Options(
        request_id=args.request_id or default_request_id(),
        input_format=("mbox" if args.pst.is_dir() or args.pst.suffix.lower() == ".mbox" else "pst") if args.input_format == "auto" else args.input_format,
        display_timezone=args.display_timezone,
        export_source=args.export_source,
        search_query=args.search_query,
        export_description=args.export_description,
        order=args.order,
        bates=BatesSpec(prefix=args.bates_prefix, start=args.bates_start),
        stamp_bates=args.stamp_bates,
        volume_pages=args.volume_pages,
        dedup_messages=args.dedup_messages,
        dedup_attachments=args.dedup_attachments,
        convert_attachments=args.convert_attachments,
        keep_natives=args.keep_natives,
        expand_zips=args.expand_zips,
        terms_file=args.terms,
        decisions_file=args.decisions,
        withheld_by=" ".join(args.withheld_by.split()),
        patterns=[p.strip().lower() for p in args.patterns.split(",") if p.strip()],
        student_id_regex=args.student_id_regex,
        overlay=_parse_overlay(args.overlay),
        survey=args.survey,
        triage=args.triage,
        jobs=args.jobs,
        appendix_over=args.appendix_over,
    )


def _print_volume_summary(stats: dict) -> None:
    detail = [f"{stats['duplicates_removed']:,} duplicate copies removed", f"{stats['messages']:,} in volumes"]
    if stats["calendar_items"]:
        detail.append(f"{stats['calendar_items']:,} of them calendar items")
    print(f"Messages found: {stats['messages_found']:,} ({', '.join(detail)})")
    # Message volumes and attachment volumes are counted apart, so a few
    # message volumes are not lost behind many attachment volumes.
    appendix = stats.get("appendix_volumes", 0)
    print(f"Volumes: {stats.get('volumes', 0)}", end="")
    if stats.get("bates_first"):
        print(f", pages {stats['bates_first']} to {stats.get('bates_volumes_last', stats['bates_last'])}", end="")
    elif stats.get("pages"):
        print(f", {stats.get('volume_pages', stats['pages']):,} pages (unnumbered)", end="")
    print()
    if appendix:
        items = stats.get("appendix_items", 0)
        pages = stats.get("appendix_pages", 0)
        where = (
            f"pages {stats['bates_appendix_first']} to {stats['bates_last']}"
            if stats.get("bates_appendix_first")
            else f"{pages:,} pages (unnumbered)"
        )
        print(f"Attachment volumes: {appendix}, {where} ({items:,} item{'' if items == 1 else 's'})")
    parts = [f"{stats['attachments_converted']:,} converted", f"{stats['attachments_crossref']:,} cross-referenced"]
    if stats.get("attachments_appendix"):
        parts.insert(1, f"{stats['attachments_appendix']:,} in attachment volumes")
    if stats.get("attachments_containers"):
        parts.append(f"{stats['attachments_containers']:,} zip archives expanded")
    if stats.get("attachments_withheld"):
        parts.append(f"{stats['attachments_withheld']:,} withheld by decision")
    if stats.get("attachments_omitted"):
        parts.append(f"{stats['attachments_omitted']:,} not included by decision")
    parts.append(f"{stats['attachments_exceptions']:,} could not be converted")
    print(f"Attachments: {stats['attachments']:,} ({', '.join(parts)})")
    print(f"Skipped non-mail items: {stats['skipped_non_mail']}")
    if stats["blocked_urls"]:
        print(f"Remote images blocked (offline rendering): {stats['blocked_urls']}")
    if stats["rtf_fallbacks"]:
        print(f"RTF-body fallbacks (rendered from plain text): {stats['rtf_fallbacks']}")
    if stats.get("render_fallbacks"):
        header_only = len(stats.get("render_failures") or [])
        print(f"Messages shown unformatted because rendering failed: {stats['render_fallbacks']}"
              + (f" ({header_only} header only)" if header_only else ""))
    if stats.get("offpage_fallbacks"):
        print(f"Messages shown unformatted because their layout put text outside the page: {stats['offpage_fallbacks']}")
    if stats.get("offpage_text"):
        print(f"Pages still carrying text or images outside the page edge: {stats['offpage_text']} (the -v log names them)")
    cov = stats.get("coverage")
    if cov is not None:
        print(f"Redaction marks added: {cov.total}")
        misses = len(cov.terms_without_hits)
        if misses:
            print(f"Terms with no hits: {misses} (listed in '{report.PREMARK_DETAIL_NAME}')")
    if stats.get("manifest"):
        print(f"Manifest: {stats['manifest']}")
    print(f"Errors: {stats['errors']}")


def _print_survey_summary(stats: dict) -> None:
    budget = stats["page_budget"]
    print(
        f"Messages found: {stats['messages_found']:,} ({stats['duplicates_removed']:,} duplicate copies removed, "
        f"{budget.messages:,} on {budget.message_pages:,} pages)"
    )
    print(
        f"Attachments: {budget.attachment_slots:,} attached, {budget.distinct_attachments:,} different files, "
        f"{budget.attachment_pages:,} pages, {budget.exceptions:,} could not be converted"
    )
    print(
        f"Projected: {budget.total_pages:,} pages in {budget.volumes} volume{'' if budget.volumes == 1 else 's'} "
        f"of up to {budget.volume_pages:,} pages"
    )
    if budget.appendix_volumes:
        print(
            f"Attachment volumes: {budget.appendix_volumes} ({budget.appendix_items:,} "
            f"item{'' if budget.appendix_items == 1 else 's'}, {budget.appendix_pages:,} of those pages)"
        )
    top = survey.largest(budget, 1)
    if top:
        share = survey.share_text(top[0].pages_in_volumes, budget.total_pages)
        print(f"Largest attachment: {top[0].pages_in_volumes:,} pages ({share} of the total)")
    print(f"Survey: {stats['survey_pdf']}")
    print(f"Every attachment: {stats['survey_csv']}")
    print(f"Errors: {stats['errors']}")


def _print_triage_summary(stats: dict) -> None:
    print(f"Messages found: {stats['messages_found']:,} ({stats['duplicates_removed']:,} duplicate copies removed, {stats['messages']:,} in the triage volumes)")
    parts = [f"{stats['native_files']:,} different files written"]
    if stats.get("attachments_containers"):
        parts.append(f"{stats['attachments_containers']:,} zip archives expanded")
    if stats.get("attachments_withheld"):
        parts.append(f"{stats['attachments_withheld']:,} withheld by decision")
    if stats.get("attachments_omitted"):
        parts.append(f"{stats['attachments_omitted']:,} not included by decision")
    print(f"Attachments: {stats['attachments']:,} ({', '.join(parts)})")
    print(f"Triage volumes: {stats.get('volumes', 0)}, {stats.get('volume_pages', 0):,} pages (unnumbered, not for redaction)")
    print(f"Original files: {stats['natives_dir']}")
    print(f"Attachment index for review: {stats['index_csv']}")
    print(f"Errors: {stats['errors']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    # WeasyPrint logs every quirky CSS property in real-world mail and every
    # blocked remote image at WARNING/ERROR; the blocked images are counted in
    # the summary instead, and genuine render failures surface via our own
    # error handling, so its logger only causes alarm.
    logging.getLogger("weasyprint").setLevel(logging.CRITICAL)
    logging.getLogger("fontTools").setLevel(logging.WARNING)

    if not args.pst.exists():
        parser.error(f"input file not found: {args.pst}")
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.volume_pages < 1:
        parser.error("--volume-pages must be at least 1")
    if args.bates_start < 0:
        parser.error("--bates-start must not be negative")
    if args.terms is not None and not args.terms.is_file():
        parser.error(f"terms file not found: {args.terms}")
    if args.separate:
        parser.error(SEPARATE_REMOVED)
    if args.appendix_over is not None and args.appendix_over < 0:
        parser.error("--appendix-over must not be negative")
    if args.triage and args.survey:
        parser.error("--triage and --survey are different runs: --survey counts converted pages, --triage converts nothing")
    if args.triage and args.appendix_over is not None:
        parser.error("--appendix-over cannot be combined with --triage (nothing is converted, so nothing moves)")
    if args.decisions is not None:
        if not args.decisions.is_file():
            parser.error(f"decisions file not found: {args.decisions}")
        try:
            decisions.load(args.decisions)  # a bad row stops the run now, not after an hour of conversion
        except ValueError as exc:
            parser.error(str(exc))
    try:
        options = options_from_args(args)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if options.display_timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(options.display_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            parser.error(f"unknown display timezone: {options.display_timezone}")
    if options.input_format == "mbox":
        from .mbox import discover
        try:
            discover(args.pst)
        except ValueError as exc:
            parser.error(str(exc))
        if args.search_report is not None:
            parser.error("--search-report reads Purview metadata for PST only; use --search-query for MBOX")
    elif not args.pst.is_file():
        parser.error("PST input must be a file")
    if args.search_report is not None:
        try:
            search = searchreport.load(args.search_report)  # before extraction, like --decisions
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        if searchreport.maybe_truncated(search):
            print(
                f"The search report's query is exactly {searchreport.TRUNCATED_LENGTH} characters, the length at which "
                "some Purview reports cut text off: check it against the search in Purview",
                file=sys.stderr,
            )
        options = replace(options, search_report=search)
    if not args.request_id:
        print(
            f"No --request-id given: using '{options.request_id}' on START HERE and in the PDF titles "
            "(the archive's file name is never used, since it could be a person's name)",
            file=sys.stderr,
        )
    if options.survey:
        ignored = [flag for flag, on in (("--terms", options.terms_file), ("--patterns", options.patterns), ("--keep-natives", options.keep_natives)) if on]
        if ignored:
            print(f"--survey writes no volumes, so {', '.join(ignored)} {'is' if len(ignored) == 1 else 'are'} ignored", file=sys.stderr)
        options = replace(options, terms_file=None, patterns=[], keep_natives=False)
    if options.triage:
        ignored = [flag for flag, on in (("--terms", options.terms_file), ("--patterns", options.patterns)) if on]
        if ignored:
            print(f"--triage volumes are not for redaction, so {', '.join(ignored)} {'is' if len(ignored) == 1 else 'are'} ignored", file=sys.stderr)
        if not options.dedup_attachments:
            print("--triage writes each different file once, so --no-dedup-attachments is ignored", file=sys.stderr)
        options = triage_options(options)
    if options.patterns:
        from . import premark

        try:
            premark.resolve_patterns(options.patterns, options)
        except ValueError as exc:
            parser.error(str(exc))
    args.output_dir.mkdir(parents=True, exist_ok=True)

    def run(work: Path) -> dict:
        stage("Extracting messages from the archive")
        extract_root = args.pst if options.input_format == "mbox" else extract.extract_pst(args.pst, work / "extract")
        return build_volumes(extract_root, args.output_dir, options, work / "build")

    try:
        if args.work_dir is not None:
            args.work_dir.mkdir(parents=True, exist_ok=True)
            stats = run(args.work_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="pst2pdf-") as tmp:
                stats = run(Path(tmp))
    except extract.ExtractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BrokenProcessPool:
        print(
            "error: a PDF render worker process died unexpectedly; "
            "retry with --jobs 1 to isolate the offending message",
            file=sys.stderr,
        )
        return 2

    if options.survey:
        _print_survey_summary(stats)
    elif options.triage:
        _print_triage_summary(stats)
    else:
        _print_volume_summary(stats)
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
