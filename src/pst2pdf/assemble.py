"""Assemble review volumes: plan where every page lands, then merge, stamp,
label and outline the volume PDFs.

Two entry points, per the contract in docs/DESIGN.md:

* :func:`plan` walks the bundles in seq order and assigns a Bates number to
  every page before anything is written, so the index can quote ranges and
  cross-reference pages can point at their first occurrence.
* :func:`write_volumes` merges the PDFs in plan order with pypdf, stamps the
  Bates label in the visual bottom-right of every page (a reportlab overlay
  drawn in the page's own coordinate system, so ``/Rotate`` and non-Letter
  pages from converted attachments are handled), sets PDF page labels equal
  to the Bates labels, builds the outline, and writes the metadata.
* :func:`write_appendix` does the same for the appendix volumes
  (``--appendix-over``): attachments moved out of the message stream, each
  behind a one-page header sheet, numbered on from the last message page.

Planning always assigns Bates numbers, because that is how volume boundaries
and page accounting are worked out. ``Options.stamp_bates`` decides only
whether the numbers are printed: with it off no stamp is drawn and no page
labels are written, and report.py leaves every page reference out of the
index, the exceptions table and the cross-reference pages.

Everything here is deterministic: the same inputs produce byte-identical
volumes, which is what lets the manifest's sha256 mean something.
"""

from __future__ import annotations

import io
import logging
from dataclasses import replace
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    RectangleObject,
    StreamObject,
    TextStringObject,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

from . import __version__
from .convert import SANITIZE_EXCLUDED_KEYS
from .model import AppendixItem, AttachmentEntry, MessageBundle, Options, PageRange, Plan, Volume

log = logging.getLogger(__name__)

STAMP_FONT_SIZE = 8
STAMP_MARGIN = 18  # points from the visual right and bottom edges (0.25 in)
STAMP_PADDING = 2  # points of white backing around the label
STAMP_FONT_NAME = "DejaVuSans"
STAMP_XOBJECT = "pst2pdfBates"  # resource name of the stamp form on every page

# DejaVu Sans is guaranteed in the container (fonts-dejavu-core); on a host
# it may live elsewhere or not at all, in which case Helvetica (a standard
# PDF font, never embedded) is used instead.
FONT_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    Path("/usr/local/share/fonts/DejaVuSans.ttf"),
    Path("/Library/Fonts/DejaVuSans.ttf"),
    Path("~/Library/Fonts/DejaVuSans.ttf"),
)

_stamp_font: str | None = None


class AssembleError(RuntimeError):
    """A volume could not be assembled as planned (missing or mis-sized input)."""


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def volume_filename(number: int, total: int, stem: str = "Volume") -> str:
    width = max(2, len(str(total)))
    return f"{stem} {number:0{width}d} of {total:0{width}d}.pdf"


APPENDIX_STEM = "Attachments"
#: --triage volumes are for reading, never for redaction or release, and
#: their names say so.
TRIAGE_STEM = "Triage copy - Volume"


def appendix_filename(number: int, total: int) -> str:
    """'Attachments 01 of 02.pdf'."""
    return volume_filename(number, total, APPENDIX_STEM)


class _PageCounter:
    """Counts pages of PDFs on disk, once per path."""

    def __init__(self) -> None:
        self._cache: dict[Path, int] = {}

    def __call__(self, path: Path) -> int:
        path = Path(path)
        if path not in self._cache:
            self._cache[path] = len(PdfReader(str(path)).pages)
        return self._cache[path]


def _entry_pages(entry: AttachmentEntry, count: _PageCounter) -> int:
    """Pages an attachment slot occupies: separator/placeholder/cross-ref plus content."""
    if entry.appendix_item is not None:
        # Pointer page into the appendix. Like a cross-reference it quotes a
        # range known only after planning, and is always one page.
        return 1
    if entry.duplicate_of is not None:
        # Cross-reference page. It is rendered inside the plan loop (its text
        # quotes a range only known after planning), and is always one page.
        return 1
    separator = count(entry.separator_pdf) if entry.separator_pdf else 0
    conv = entry.converted
    if conv is None or conv.status == "placeholder" or conv.pdf_path is None:
        # Placeholder: the rendered page if we have it, otherwise exactly one.
        return separator or 1
    pages = conv.page_count
    if pages <= 0:
        pages = count(conv.pdf_path)
    return separator + pages


def _item_pages(item: AppendixItem, count: _PageCounter) -> tuple[int, int]:
    """(header sheet pages, content pages) of an appendix item."""
    header = count(item.header_pdf) if item.header_pdf else 1
    pages = item.converted.page_count
    if pages <= 0 and item.converted.pdf_path is not None:
        pages = count(item.converted.pdf_path)
    return header, pages


def _split(sizes: list[tuple[int, int]], number: int, limit: int) -> tuple[list[list], int]:
    """Group (key, pages) runs into volumes, same rule as the message volumes:
    close a volume when the next run would push it past *limit*, unless it is
    still empty. Returns ([first bates, pages, [keys]] per volume, next bates)."""
    raw: list[list] = []
    current: list | None = None
    for key, pages in sizes:
        if current is not None and current[2] and current[1] + pages > limit:
            raw.append(current)
            current = None
        if current is None:
            current = [number, 0, []]
        current[1] += pages
        current[2].append(key)
        number += pages
    if current is not None:
        raw.append(current)
    return raw, number


def plan(
    bundles: list[MessageBundle],
    options: Options,
    front_matter_pages: int = 0,
    appendix: list[AppendixItem] | None = None,
) -> Plan:
    """Assign Bates numbers and volume boundaries without writing anything.

    Front matter (the index) comes first and lives in volume 1. A volume
    closes when the next bundle would push it past ``options.volume_pages``,
    unless it holds no bundle yet: a single oversized bundle still gets its
    own volume, and volume 1 always takes the first bundle even when the
    front matter alone is large.

    *appendix* items (``--appendix-over``) are laid out after the last message
    volume, in item order, with the Bates sequence carrying straight on, and
    split into appendix volumes by the same rule. Each item is its header
    sheet followed by its content pages.

    ``Volume.path`` is the bare filename here; :func:`write_volumes` resolves
    it against the output directory.
    """
    bundles = sorted(bundles, key=lambda b: b.seq)
    count = _PageCounter()
    limit = max(1, options.volume_pages)
    number = options.bates.start

    front_matter: PageRange | None = None
    if front_matter_pages > 0:
        front_matter = PageRange(number, number + front_matter_pages - 1)
        number += front_matter_pages

    message_ranges: dict[int, PageRange] = {}
    attachment_ranges: dict[tuple[int, int], PageRange] = {}
    bundle_ranges: dict[int, PageRange] = {}

    # Each open volume: [first bates, page count, [seqs]]
    volumes_raw: list[list] = []
    current: list | None = None
    if front_matter is not None:
        current = [front_matter.start, front_matter_pages, []]

    for bundle in bundles:
        seq = bundle.seq
        entry_pages = [_entry_pages(e, count) for e in bundle.attachments]
        pages = bundle.message_pages + sum(entry_pages)

        if current is not None and current[2] and current[1] + pages > limit:
            volumes_raw.append(current)
            current = None
        if current is None:
            current = [number, 0, []]

        message_ranges[seq] = PageRange(number, number + bundle.message_pages - 1)
        cursor = number + bundle.message_pages
        for entry, n in zip(bundle.attachments, entry_pages):
            attachment_ranges[(seq, entry.index)] = PageRange(cursor, cursor + n - 1)
            cursor += n
        bundle_ranges[seq] = PageRange(number, cursor - 1)
        number = cursor

        current[1] += pages
        current[2].append(seq)

    if current is not None:
        volumes_raw.append(current)

    total = len(volumes_raw)
    volumes = [
        Volume(
            number=i,
            total=total,
            path=Path(volume_filename(i, total, TRIAGE_STEM if options.triage else "Volume")),
            bates=PageRange(start, start + pages - 1),
            first_seq=seqs[0] if seqs else 0,
            last_seq=seqs[-1] if seqs else 0,
            page_count=pages,
        )
        for i, (start, pages, seqs) in enumerate(volumes_raw, start=1)
    ]
    result = Plan(
        volumes=volumes,
        message_ranges=message_ranges,
        attachment_ranges=attachment_ranges,
        bundle_ranges=bundle_ranges,
        front_matter=front_matter,
    )
    if appendix:
        _plan_appendix(result, appendix, number, limit, count)
    return result


def _plan_appendix(
    result: Plan, items: list[AppendixItem], number: int, limit: int, count: _PageCounter
) -> None:
    """Lay the appendix items out from Bates *number* on, into *result*."""
    sizes = []
    for item in sorted(items, key=lambda i: i.number):
        header, pages = _item_pages(item, count)
        sizes.append((item.number, header + pages))
    raw, _ = _split(sizes, number, limit)
    for item_number, pages in sizes:
        result.appendix_ranges[item_number] = PageRange(number, number + pages - 1)
        number += pages
    total = len(raw)
    result.appendix_volumes = [
        Volume(
            number=i,
            total=total,
            path=Path(appendix_filename(i, total)),
            bates=PageRange(start, start + pages - 1),
            first_seq=keys[0],  # item numbers, not message seqs
            last_seq=keys[-1],
            page_count=pages,
        )
        for i, (start, pages, keys) in enumerate(raw, start=1)
    ]


# --------------------------------------------------------------------------- #
# Stamping
# --------------------------------------------------------------------------- #


def stamp_font_name() -> str:
    """Register DejaVu Sans with reportlab if a copy exists; else Helvetica."""
    global _stamp_font
    if _stamp_font is None:
        _stamp_font = "Helvetica"
        for candidate in FONT_CANDIDATES:
            path = candidate.expanduser()
            if not path.is_file():
                continue
            try:
                pdfmetrics.registerFont(TTFont(STAMP_FONT_NAME, str(path)))
            except Exception as exc:  # pragma: no cover - depends on host fonts
                log.warning("Could not load stamp font %s: %s", path, exc)
                continue
            _stamp_font = STAMP_FONT_NAME
            break
        if _stamp_font == "Helvetica":
            log.info("DejaVu Sans not found; stamping Bates labels in Helvetica")
    return _stamp_font


def _inherited(page, key: str, default=None):
    """Read a page attribute, walking up /Parent for inheritable keys."""
    node = page
    seen = 0
    while node is not None and seen < 64:
        if key in node:
            return node[key].get_object()
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
        seen += 1
    return default


def _page_rotation(page) -> int:
    rot = _inherited(page, "/Rotate", 0)
    try:
        rot = int(round(float(rot) / 90.0)) * 90
    except (TypeError, ValueError):
        rot = 0
    return rot % 360


def _visual_box(page) -> tuple[float, float, float, float, int]:
    """(x0, y0, width, height, rotation) of the page's crop box in user space."""
    box = page.cropbox
    x0, y0 = float(box.left), float(box.bottom)
    return x0, y0, float(box.width), float(box.height), _page_rotation(page)


def _draw_stamp(c: rl_canvas.Canvas, x0: float, y0: float, w: float, h: float,
                rotation: int, label: str, font: str) -> None:
    """Draw ``label`` at the visual bottom-right of a page.

    The canvas is put into "visual" coordinates first: a transform that maps
    the displayed page (after the viewer applies /Rotate clockwise) onto the
    unrotated user space, so the text is positioned and rotated so it reads
    upright once the viewer rotates the page.

    A white rectangle (fill only, ``STAMP_PADDING`` around the glyph box) is
    drawn first so the number stays legible on a full-bleed scan or photo.
    """
    c.saveState()
    c.translate(x0, y0)
    if rotation == 90:
        c.transform(0, 1, -1, 0, w, 0)
        vw, vh = h, w
    elif rotation == 180:
        c.transform(-1, 0, 0, -1, w, h)
        vw, vh = w, h
    elif rotation == 270:
        c.transform(0, -1, 1, 0, 0, h)
        vw, vh = h, w
    else:
        vw, vh = w, h
    c.setFont(font, STAMP_FONT_SIZE)
    width = pdfmetrics.stringWidth(label, font, STAMP_FONT_SIZE)
    ascent, descent = pdfmetrics.getAscentDescent(font, STAMP_FONT_SIZE)
    x_right, baseline = vw - STAMP_MARGIN, STAMP_MARGIN
    c.setFillGray(1)
    c.rect(
        x_right - width - STAMP_PADDING,
        baseline + descent - STAMP_PADDING,
        width + 2 * STAMP_PADDING,
        ascent - descent + 2 * STAMP_PADDING,
        stroke=0,
        fill=1,
    )
    c.setFillGray(0)
    c.drawRightString(x_right, baseline, label)
    c.restoreState()


def _build_overlay(pages, labels: list[str]) -> PdfReader:
    """One reportlab document with a stamp page matching every volume page.

    A single document keeps one embedded font subset shared by all pages and
    is far cheaper than a canvas per page. ``invariant`` pins reportlab's
    dates and file identifier so output is reproducible.
    """
    font = stamp_font_name()
    buf = io.BytesIO()
    # initialFontName keeps reportlab's page preamble from referencing
    # Helvetica as well, so each stamp form carries exactly one font.
    c = rl_canvas.Canvas(
        buf,
        invariant=1,
        pageCompression=0,
        initialFontName=font,
        initialFontSize=STAMP_FONT_SIZE,
    )
    c.setTitle("")
    c.setAuthor("")
    c.setSubject("")
    c.setCreator("")
    for page, label in zip(pages, labels):
        x0, y0, w, h, rotation = _visual_box(page)
        c.setPageSize((x0 + w, y0 + h))
        _draw_stamp(c, x0, y0, w, h, rotation, label, font)
        c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf)


def _stream(data: bytes) -> StreamObject:
    obj = StreamObject()
    obj.set_data(data)
    return obj


def stamp_pages(writer: PdfWriter, labels: list[str]) -> None:
    """Stamp every page of ``writer`` with its label (one label per page).

    Each reportlab stamp page becomes a Form XObject carrying its own
    resources, and the page's content becomes ``[q, original..., Q, Do]``.
    The original content streams are left byte for byte as they were (never
    parsed or re-encoded), nothing is orphaned in the writer, and no resource
    names have to be rewritten, so this stays fast on thousand-page volumes.
    """
    pages = list(writer.pages)
    if len(pages) != len(labels):
        raise AssembleError(f"{len(labels)} labels for {len(pages)} pages")
    if not pages:
        return
    overlay = _build_overlay(pages, labels)
    q_ref = writer._add_object(_stream(b"q\n"))
    unq_ref = writer._add_object(_stream(b"Q\n"))
    do_ref = writer._add_object(_stream(f"q /{STAMP_XOBJECT} Do Q\n".encode()))

    for page, stamp in zip(pages, overlay.pages):
        form = _stream(stamp["/Contents"].get_object().get_data())
        form[NameObject("/Type")] = NameObject("/XObject")
        form[NameObject("/Subtype")] = NameObject("/Form")
        form[NameObject("/BBox")] = RectangleObject(tuple(page.cropbox))
        form[NameObject("/Resources")] = stamp["/Resources"].get_object().clone(writer)
        form_ref = writer._add_object(form)

        # The page gets its own shallow copy of its resources (they may be
        # shared between pages or inherited), with the stamp added.
        resources = DictionaryObject()
        existing = _inherited(page, "/Resources")
        if isinstance(existing, DictionaryObject):
            resources.update(existing)
        xobjects = DictionaryObject()
        if "/XObject" in resources:
            xobjects.update(resources["/XObject"].get_object())
        xobjects[NameObject("/" + STAMP_XOBJECT)] = form_ref
        resources[NameObject("/XObject")] = xobjects
        page[NameObject("/Resources")] = resources

        contents = page.get("/Contents")
        refs: list = []
        if contents is not None:
            obj = contents.get_object()
            if isinstance(obj, ArrayObject):
                refs.extend(obj)
            elif isinstance(contents, IndirectObject):
                refs.append(contents)
            elif getattr(obj, "indirect_reference", None) is not None:
                refs.append(obj.indirect_reference)
            else:
                refs.append(writer._add_object(obj))
        page[NameObject("/Contents")] = ArrayObject([q_ref, *refs, unq_ref, do_ref])


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _set_page_labels(writer: PdfWriter, labels: list[str]) -> None:
    """Give every page a literal label (prefix only, no numbering style).

    The PDF page-label styles cannot zero-pad, so each page gets its own
    entry whose prefix is the full Bates label. Written straight into
    ``/PageLabels`` because ``PdfWriter.set_page_label`` re-walks the tree on
    every call.
    """
    nums = ArrayObject()
    for index, label in enumerate(labels):
        nums.append(NumberObject(index))
        nums.append(DictionaryObject({NameObject("/P"): TextStringObject(label)}))
    writer.root_object[NameObject("/PageLabels")] = DictionaryObject(
        {NameObject("/Nums"): nums}
    )


def outline_title(bundle: MessageBundle) -> str:
    """Seq and date only. Never the subject or a name: outlines are not redacted."""
    date = bundle.model.date
    when = f"{date:%Y-%m-%d %H:%M}" if date is not None else "undated"
    return f"{bundle.seq:04d} · {when}"


def _append_pdf(writer: PdfWriter, path: Path, what: str) -> int:
    """Copy every page of ``path`` into the writer. Returns pages added.

    The same page-level exclusions as the attachment sanitizer apply, since
    LibreOffice output and rendered message pages all come through here.
    """
    path = Path(path)
    if not path.is_file():
        raise AssembleError(f"{what}: missing PDF {path}")
    try:
        reader = PdfReader(str(path))
        n = 0
        for page in reader.pages:
            writer.add_page(page, excluded_keys=SANITIZE_EXCLUDED_KEYS)
            n += 1
    except AssembleError:
        raise
    except Exception as exc:
        raise AssembleError(f"{what}: cannot read {path}: {exc}") from exc
    return n


def _expect(writer: PdfWriter, volume: Volume, rng: PageRange, what: str) -> None:
    """Assert the writer's page count agrees with the plan after ``what``."""
    have = len(writer.pages)
    want = rng.end - volume.bates.start + 1
    if have != want:
        raise AssembleError(
            f"{what}: volume {volume.number} has {have} pages after it, "
            f"plan expected {want} (Bates {rng.start}-{rng.end}); "
            "an input PDF has a different page count than planned"
        )


def _write_one(
    volume: Volume,
    plan_: Plan,
    bundles: list[MessageBundle],
    index_pdf: Path | None,
    options: Options,
    output_dir: Path,
) -> Volume:
    writer = PdfWriter()
    nn = volume_filename(volume.number, volume.total)[len("Volume ") : -len(".pdf")]

    # 1. Pages, in plan order, with a running check against the plan.
    outline_pages: list[tuple[MessageBundle, int, list[tuple[AttachmentEntry, int]]]] = []
    front_matter_page: int | None = None
    if volume.number == 1 and plan_.front_matter is not None:
        if index_pdf is None:
            raise AssembleError("plan has front matter but no index PDF was given")
        front_matter_page = 0
        _append_pdf(writer, index_pdf, "front matter")
        _expect(writer, volume, plan_.front_matter, "front matter")

    if volume.first_seq:
        members = [b for b in bundles if volume.first_seq <= b.seq <= volume.last_seq]
    else:
        members = []
    for bundle in members:
        seq = bundle.seq
        message_page = len(writer.pages)
        _append_pdf(writer, bundle.message_pdf, f"message {seq}")
        _expect(writer, volume, plan_.message_ranges[seq], f"message {seq}")
        entries: list[tuple[AttachmentEntry, int]] = []
        for entry in bundle.attachments:
            what = f"message {seq} attachment {entry.index}"
            entries.append((entry, len(writer.pages)))
            if entry.appendix_item is not None:
                if entry.separator_pdf is None:
                    raise AssembleError(f"{what}: appendix pointer page was not rendered")
                _append_pdf(writer, entry.separator_pdf, f"{what} appendix pointer")
            elif entry.duplicate_of is not None:
                if entry.separator_pdf is None:
                    raise AssembleError(f"{what}: cross-reference page was not rendered")
                _append_pdf(writer, entry.separator_pdf, f"{what} cross-reference")
            else:
                if entry.separator_pdf is not None:
                    _append_pdf(writer, entry.separator_pdf, f"{what} separator")
                conv = entry.converted
                if conv is not None and conv.status != "placeholder" and conv.pdf_path is not None:
                    _append_pdf(writer, conv.pdf_path, f"{what} content")
                elif entry.separator_pdf is None:
                    raise AssembleError(f"{what}: placeholder page was not rendered")
            _expect(writer, volume, plan_.attachment_ranges[(seq, entry.index)], what)
        outline_pages.append((bundle, message_page, entries))

    labels = _check_and_stamp(writer, volume, options)

    # 3. Outline: volume root, one child per message, grandchildren per attachment.
    root = writer.add_outline_item(f"Volume {nn}", 0)
    if front_matter_page is not None:
        writer.add_outline_item("Index", front_matter_page, parent=root)
    for bundle, message_page, entries in outline_pages:
        item = writer.add_outline_item(outline_title(bundle), message_page, parent=root)
        for entry, page_index in entries:
            writer.add_outline_item(f"Attachment {entry.index}", page_index, parent=item)

    name = f"Triage copy - Volume {nn}" if options.triage else f"Volume {nn}"
    return _save(writer, volume, options, output_dir / Path(volume.path).name, name, labels)


def _check_and_stamp(writer: PdfWriter, volume: Volume, options: Options) -> list[str]:
    """Final page-count check against the plan, then (step 2) Bates stamps and
    page labels. With ``stamp_bates`` off the pages are left unnumbered:
    nothing is drawn and no page labels are written, so Acrobat falls back to
    each volume's own 1..n page positions. Returns the planned labels."""
    page_count = len(writer.pages)
    if page_count != volume.page_count:
        raise AssembleError(
            f"volume {volume.number}: assembled {page_count} pages, planned {volume.page_count}"
        )
    labels = [options.bates.label(volume.bates.start + i) for i in range(page_count)]
    if options.stamp_bates:
        stamp_pages(writer, labels)
        _set_page_labels(writer, labels)
    return labels


def _save(writer: PdfWriter, volume: Volume, options: Options, path: Path, name: str, labels: list[str]) -> Volume:
    """Step 4, metadata (title and producer only: no author, subject,
    keywords, dates), then write the file."""
    writer.add_metadata(
        {
            "/Title": f"{options.request_id} — {name}",
            "/Producer": f"pst2pdf {__version__}",
        }
    )
    page_count = len(writer.pages)
    with open(path, "wb") as fh:
        writer.write(fh)
    span = f", {labels[0]}-{labels[-1]}" if options.stamp_bates and labels else ""
    log.info("Wrote %s (%d pages%s)", path.name, page_count, span)
    return replace(volume, path=path, page_count=page_count)


def write_volumes(
    plan_: Plan,
    bundles: list[MessageBundle],
    index_pdf: Path | None,
    options: Options,
    output_dir: Path,
) -> list[Volume]:
    """Write every planned volume into ``output_dir`` and return them.

    The returned volumes carry absolute paths and the real page counts; the
    plan itself is not modified.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    bundles = sorted(bundles, key=lambda b: b.seq)
    return [
        _write_one(volume, plan_, bundles, index_pdf, options, output_dir)
        for volume in plan_.volumes
    ]


# --------------------------------------------------------------------------- #
# Appendix volumes (--appendix-over)
# --------------------------------------------------------------------------- #


def appendix_outline_title(item: AppendixItem, bundles_by_seq: dict[int, MessageBundle]) -> str:
    """'Item 14 · message 0012 · 2024-03-15 09:12': the item number and the
    first message that carried it. Never the filename: outlines are not redacted."""
    title = f"Item {item.number}"
    if item.carriers:
        bundle = bundles_by_seq.get(item.carriers[0][0])
        if bundle is not None:
            title += f" · message {outline_title(bundle)}"
    return title


def _write_appendix_one(
    volume: Volume,
    plan_: Plan,
    items: list[AppendixItem],
    bundles_by_seq: dict[int, MessageBundle],
    options: Options,
    output_dir: Path,
) -> Volume:
    writer = PdfWriter()
    name = appendix_filename(volume.number, volume.total)[: -len(".pdf")]
    starts: list[tuple[AppendixItem, int]] = []
    for item in items:
        if not volume.first_seq <= item.number <= volume.last_seq:
            continue
        what = f"appendix item {item.number}"
        starts.append((item, len(writer.pages)))
        if item.header_pdf is None:
            raise AssembleError(f"{what}: header sheet was not rendered")
        _append_pdf(writer, item.header_pdf, f"{what} header sheet")
        conv = item.converted
        if conv.pdf_path is None:
            raise AssembleError(f"{what}: has no converted pages")
        _append_pdf(writer, conv.pdf_path, f"{what} content")
        _expect(writer, volume, plan_.appendix_ranges[item.number], what)

    labels = _check_and_stamp(writer, volume, options)

    root = writer.add_outline_item(name, 0)
    for item, page_index in starts:
        writer.add_outline_item(appendix_outline_title(item, bundles_by_seq), page_index, parent=root)

    return _save(writer, volume, options, output_dir / appendix_filename(volume.number, volume.total), name, labels)


def write_appendix(
    plan_: Plan,
    items: list[AppendixItem],
    bundles: list[MessageBundle],
    options: Options,
    output_dir: Path,
) -> list[Volume]:
    """Write every planned appendix volume into ``output_dir`` and return them.

    Same guarantees as :func:`write_volumes`: a running check against the
    plan, Bates stamps and page labels (unless ``stamp_bates`` is off), a
    content-free outline (one entry per item), and Title-only metadata.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    items = sorted(items, key=lambda i: i.number)
    by_seq = {b.seq: b for b in bundles}
    return [
        _write_appendix_one(volume, plan_, items, by_seq, options, output_dir)
        for volume in plan_.appendix_volumes
    ]
