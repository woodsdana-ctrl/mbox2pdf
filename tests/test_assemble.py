"""Tests for assemble.py: planning, splitting, Bates stamps, labels, outlines.

Fixture PDFs are built with reportlab (deterministic via ``invariant``) and
``/Rotate`` is applied afterwards with pypdf, so rotated fixtures look like
what LibreOffice and scanners produce: an unrotated MediaBox plus /Rotate.
(reportlab's own ``setPageRotation`` swaps the MediaBox instead.)
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import pdfplumber
import pytest
import reportlab
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas

from pst2pdf import __version__, assemble
from pst2pdf.assemble import AssembleError, plan, write_volumes
from pst2pdf.model import (
    AppendixItem,
    Attachment,
    AttachmentEntry,
    BatesSpec,
    ConvertedAttachment,
    MessageBundle,
    MessageModel,
    Options,
    PageRange,
)

LETTER = (letter[0], letter[1], 0)
LANDSCAPE = (letter[1], letter[0], 0)
A4_ROT90 = (A4[0], A4[1], 90)
A4_ROT270 = (A4[0], A4[1], 270)
LETTER_ROT180 = (letter[0], letter[1], 180)

SECRET = "Secret subject about Jane Doe"


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def make_pdf(path: Path, pages, bookmark: str | None = None) -> Path:
    """Write a PDF with one page per (width, height, rotate) spec."""
    c = canvas.Canvas(str(path), invariant=1)
    for i, (w, h, _rot) in enumerate(pages):
        c.setPageSize((w, h))
        c.setFont("Helvetica", 12)
        c.drawString(72, h - 72, f"{path.stem} page {i + 1}")
        if bookmark and i == 0:
            c.bookmarkPage("bm")
            c.addOutlineEntry(bookmark, "bm")
        c.showPage()
    c.save()
    if any(spec[2] for spec in pages):
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for page, spec in zip(reader.pages, pages):
            if spec[2]:
                page.rotate(spec[2])
            writer.add_page(page)
        with open(path, "wb") as fh:
            writer.write(fh)
    return path


def model(seq: int, date: datetime | None = datetime(2024, 3, 15, 9, 12, 3)) -> MessageModel:
    return MessageModel(
        source=Path(f"Inbox/{seq}.eml"),
        folder="Inbox",
        subject=SECRET,
        from_addr="Jane Doe <jane@example.com>",
        to_addrs="bob@example.com",
        cc_addrs="",
        date=date,
        body_html="<p>hi</p>",
    )


def converted(pdf_path: Path | None, pages: int, status: str = "converted") -> ConvertedAttachment:
    att = Attachment("file.docx", b"content", "application/octet-stream")
    return ConvertedAttachment(att, status, "libreoffice", pdf_path, pages)


def entry(index: int, total: int, conv=None, separator=None, duplicate_of=None) -> AttachmentEntry:
    return AttachmentEntry(
        index=index,
        total=total,
        filename="file.docx",
        size=7,
        content_type="application/octet-stream",
        sha256="0" * 64,
        converted=conv,
        duplicate_of=duplicate_of,
        separator_pdf=separator,
    )


def bundle(seq: int, message_pdf: Path, message_pages: int, attachments=(), date=datetime(2024, 3, 15, 9, 12, 3)) -> MessageBundle:
    return MessageBundle(
        seq=seq,
        model=model(seq, date),
        folders=["Inbox"],
        thread_id="abc123def456",
        message_pdf=message_pdf,
        message_pages=message_pages,
        attachments=list(attachments),
    )


def options(**kw) -> Options:
    return Options(request_id="REQ-2026-001", **kw)


@pytest.fixture
def pdfs(tmp_path):
    """A set of fixture PDFs shared by the planning tests."""
    d = tmp_path / "in"
    d.mkdir()
    return {
        "one": make_pdf(d / "one.pdf", [LETTER]),
        "two": make_pdf(d / "two.pdf", [LETTER, LETTER]),
        "three": make_pdf(d / "three.pdf", [LETTER, LETTER, LETTER]),
    }


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def test_plan_assigns_bates_numbers_front_matter_first(pdfs):
    b1 = bundle(1, pdfs["two"], 2, [
        entry(1, 2, converted(pdfs["three"], 3), separator=pdfs["one"]),
        entry(2, 2, None, separator=pdfs["one"], duplicate_of=1),
    ])
    b2 = bundle(2, pdfs["one"], 1)
    opts = options(bates=BatesSpec(prefix="ABC-", start=100))
    p = plan([b2, b1], opts, front_matter_pages=3)  # order does not matter

    assert p.front_matter == PageRange(100, 102)
    assert p.message_ranges == {1: PageRange(103, 104), 2: PageRange(110, 110)}
    assert p.attachment_ranges == {
        (1, 1): PageRange(105, 108),  # 1 separator + 3 content
        (1, 2): PageRange(109, 109),  # cross-reference: always one page
    }
    assert p.bundle_ranges == {1: PageRange(103, 109), 2: PageRange(110, 110)}
    assert len(p.volumes) == 1
    v = p.volumes[0]
    assert (v.number, v.total) == (1, 1)
    assert v.path == Path("Volume 01 of 01.pdf")
    assert v.bates == PageRange(100, 110)
    assert (v.first_seq, v.last_seq) == (1, 2)
    assert v.page_count == 11


def test_plan_without_front_matter(pdfs):
    p = plan([bundle(1, pdfs["one"], 1)], options())
    assert p.front_matter is None
    assert p.volumes[0].bates == PageRange(1, 1)
    assert p.message_ranges[1] == PageRange(1, 1)


def test_plan_splits_at_bundle_boundary(pdfs):
    bundles = [bundle(i, pdfs["three"], 3) for i in range(1, 6)]  # 5 x 3 pages
    p = plan(bundles, options(volume_pages=7), front_matter_pages=2)

    # Volume 1: front matter (2) + b1 (3) = 5; b2 would make 8 > 7.
    # Volume 2: b2 + b3 = 6; b4 would make 9. Volume 3: b4 + b5.
    assert [(v.first_seq, v.last_seq) for v in p.volumes] == [(1, 1), (2, 3), (4, 5)]
    assert [v.page_count for v in p.volumes] == [5, 6, 6]
    assert [v.bates for v in p.volumes] == [PageRange(1, 5), PageRange(6, 11), PageRange(12, 17)]
    assert [v.path.name for v in p.volumes] == [
        "Volume 01 of 03.pdf", "Volume 02 of 03.pdf", "Volume 03 of 03.pdf",
    ]
    assert all(v.total == 3 for v in p.volumes)
    # An exact fit is allowed: 7 pages in a 7-page volume.
    p = plan(bundles[:2], options(volume_pages=7), front_matter_pages=1)
    assert len(p.volumes) == 1 and p.volumes[0].page_count == 7


def test_plan_oversized_bundle_gets_its_own_volume(pdfs):
    big = bundle(2, pdfs["three"], 3, [entry(1, 1, converted(pdfs["three"], 9), separator=pdfs["one"])])
    bundles = [bundle(1, pdfs["two"], 2), big, bundle(3, pdfs["one"], 1)]
    p = plan(bundles, options(volume_pages=5))
    assert [(v.first_seq, v.last_seq) for v in p.volumes] == [(1, 1), (2, 2), (3, 3)]
    assert p.volumes[1].page_count == 13
    assert p.bundle_ranges[2] == PageRange(3, 15)

    # Front matter alone never makes a volume: the first bundle joins it even
    # when the pair is oversized.
    p = plan([big, bundle(3, pdfs["one"], 1)], options(volume_pages=5), front_matter_pages=4)
    assert [(v.first_seq, v.last_seq) for v in p.volumes] == [(2, 2), (3, 3)]
    assert p.volumes[0].page_count == 4 + 13


def test_plan_entry_page_rules(pdfs):
    placeholder_with_page = entry(1, 4, converted(None, 0, "placeholder"), separator=pdfs["two"])
    placeholder_bare = entry(2, 4, converted(None, 0, "placeholder"))
    crossref = entry(3, 4, None, separator=pdfs["three"], duplicate_of=1)
    uncounted = entry(4, 4, converted(pdfs["two"], 0))  # page_count unfilled: read the file
    p = plan([bundle(1, pdfs["one"], 1, [placeholder_with_page, placeholder_bare, crossref, uncounted])], options())
    assert p.attachment_ranges[(1, 1)] == PageRange(2, 3)  # rendered placeholder, 2 pages
    assert p.attachment_ranges[(1, 2)] == PageRange(4, 4)  # not rendered yet: one page
    assert p.attachment_ranges[(1, 3)] == PageRange(5, 5)  # cross-ref is one page even if the file has more
    assert p.attachment_ranges[(1, 4)] == PageRange(6, 7)
    assert p.volumes[0].page_count == 7


def test_plan_empty():
    p = plan([], options())
    assert p.volumes == [] and p.front_matter is None
    p = plan([], options(), front_matter_pages=2)
    assert len(p.volumes) == 1
    assert (p.volumes[0].first_seq, p.volumes[0].last_seq, p.volumes[0].page_count) == (0, 0, 2)


def test_volume_filename_widens_past_99():
    assert assemble.volume_filename(1, 4) == "Volume 01 of 04.pdf"
    assert assemble.volume_filename(7, 120) == "Volume 007 of 120.pdf"


def test_outline_title_has_seq_and_date_only(pdfs):
    assert assemble.outline_title(bundle(17, pdfs["one"], 1)) == "0017 · 2024-03-15 09:12"
    assert assemble.outline_title(bundle(3, pdfs["one"], 1, date=None)) == "0003 · undated"


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def build_scenario(tmp_path: Path):
    """Two volumes with every page shape the stamp must cope with."""
    d = tmp_path / "in"
    d.mkdir(exist_ok=True)
    index = make_pdf(d / "index.pdf", [LETTER])
    m1 = make_pdf(d / "m1.pdf", [LETTER, LETTER], bookmark=SECRET)
    sep = make_pdf(d / "sep.pdf", [LETTER])
    a1 = make_pdf(d / "a1.pdf", [LANDSCAPE])
    a2 = make_pdf(d / "a2.pdf", [A4_ROT90, A4_ROT270])
    placeholder = make_pdf(d / "placeholder.pdf", [LETTER])
    m2 = make_pdf(d / "m2.pdf", [LETTER, LETTER_ROT180])
    crossref = make_pdf(d / "crossref.pdf", [LETTER])

    b1 = bundle(1, m1, 2, [
        entry(1, 3, converted(a1, 1), separator=sep),
        entry(2, 3, converted(a2, 2, "passthrough"), separator=sep),
        entry(3, 3, converted(None, 0, "placeholder"), separator=placeholder),
    ])
    b2 = bundle(2, m2, 2, [entry(1, 1, None, separator=crossref, duplicate_of=1)], date=datetime(2024, 4, 1, 17, 5))
    opts = options(volume_pages=9)
    p = plan([b1, b2], opts, front_matter_pages=1)
    return p, [b1, b2], index, opts


def outline_titles(items) -> list:
    """Flatten a pypdf outline into nested [title, [children...]] lists."""
    out = []
    for item in items:
        if isinstance(item, list):
            out.append(outline_titles(item))
        else:
            out.append(item.title)
    return out


def test_write_volumes_end_to_end(tmp_path):
    p, bundles, index, opts = build_scenario(tmp_path)
    assert [(v.first_seq, v.last_seq, v.page_count) for v in p.volumes] == [(1, 1, 9), (2, 2, 3)]

    out = tmp_path / "out"
    volumes = write_volumes(p, bundles, index, opts, out)

    assert [v.path for v in volumes] == [out / "Volume 01 of 02.pdf", out / "Volume 02 of 02.pdf"]
    assert [v.page_count for v in volumes] == [9, 3]
    assert [v.bates for v in volumes] == [PageRange(1, 9), PageRange(10, 12)]
    # The plan is left alone; the returned volumes carry the real paths.
    assert p.volumes[0].path == Path("Volume 01 of 02.pdf")

    expected_labels = [[f"DOC-{n:06d}" for n in range(1, 10)], [f"DOC-{n:06d}" for n in range(10, 13)]]
    for volume, labels in zip(volumes, expected_labels):
        reader = PdfReader(str(volume.path))
        assert len(reader.pages) == volume.page_count
        # Page labels round-trip and equal the Bates labels.
        assert reader.page_labels == labels
        # Every page's text contains exactly its own label.
        for page, label in zip(reader.pages, labels):
            found = re.findall(r"DOC-\d{6}", page.extract_text())
            assert found == [label]
        # Metadata: title and producer only.
        meta = reader.metadata
        assert meta.title == f"{opts.request_id} — Volume 0{volume.number} of 02"
        assert meta.producer == f"pst2pdf {__version__}"
        for key in ("/Author", "/Subject", "/Keywords", "/CreationDate", "/ModDate", "/Creator"):
            assert key not in meta
        assert "/ID" not in reader.trailer

    # Outline nesting and titles: seq + date only, attachments as children.
    v1 = PdfReader(str(volumes[0].path))
    assert outline_titles(v1.outline) == [
        "Volume 01 of 02",
        ["Index", "0001 · 2024-03-15 09:12", ["Attachment 1", "Attachment 2", "Attachment 3"]],
    ]
    v2 = PdfReader(str(volumes[1].path))
    assert outline_titles(v2.outline) == [
        "Volume 02 of 02",
        ["0002 · 2024-04-01 17:05", ["Attachment 1"]],
    ]
    # Destinations land on the right pages (0-based within the volume).
    root, children = v1.outline
    assert v1.get_destination_page_number(root) == 0
    index_item, message_item, atts = children
    assert v1.get_destination_page_number(index_item) == 0
    assert v1.get_destination_page_number(message_item) == 1
    assert [v1.get_destination_page_number(a) for a in atts] == [3, 5, 8]
    root2, (message2, atts2) = v2.outline
    assert v2.get_destination_page_number(message2) == 0
    assert v2.get_destination_page_number(atts2[0]) == 2
    # Nothing sensitive leaks into the outline, not even from the source's own bookmarks.
    for reader in (v1, v2):
        titles = str(outline_titles(reader.outline))
        assert "Secret" not in titles and "Jane" not in titles and "m1" not in titles

    # Page geometry survives and the stamp is at the visual bottom-right of each page.
    rotations = [page.rotation for page in v1.pages]
    assert rotations == [0, 0, 0, 0, 0, 0, 90, 270, 0]
    assert [page.rotation for page in v2.pages] == [0, 180, 0]
    assert_stamps_bottom_right(volumes[0].path)
    assert_stamps_bottom_right(volumes[1].path)


def assert_stamps_bottom_right(path: Path) -> None:
    margin = assemble.STAMP_MARGIN
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            words = [w for w in page.extract_words() if w["text"].startswith("DOC-")]
            assert len(words) == 1, (page.page_number, words)
            w = words[0]
            # pdfplumber reports visual (post-/Rotate) coordinates with a top-left origin.
            assert w["upright"], page.page_number
            assert abs(w["x1"] - (page.width - margin)) < 1.5, (page.page_number, w["x1"], page.width)
            assert page.height - margin - 1 <= w["bottom"] <= page.height - margin + 4, (
                page.page_number, w["bottom"], page.height,
            )
            # And well clear of the left/top edges, i.e. really in the corner.
            assert w["x0"] > page.width / 2 and w["top"] > page.height / 2


def make_dark_pdf(path: Path, pages) -> Path:
    """Like make_pdf, but every page is a full-bleed near-black image, the way
    a photo or a dark scan arrives."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    c = canvas.Canvas(str(path), invariant=1)
    dark = ImageReader(Image.new("RGB", (40, 40), (20, 20, 20)))
    for w, h, _rot in pages:
        c.setPageSize((w, h))
        c.drawImage(dark, 0, 0, width=w, height=h)
        c.showPage()
    c.save()
    if any(spec[2] for spec in pages):
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for page, spec in zip(reader.pages, pages):
            if spec[2]:
                page.rotate(spec[2])
            writer.add_page(page)
        with open(path, "wb") as fh:
            writer.write(fh)
    return path


def assert_stamps_have_white_backing(path: Path) -> None:
    pad = assemble.STAMP_PADDING
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            (word,) = [w for w in page.extract_words() if w["text"].startswith("DOC-")]
            backing = [
                r for r in page.rects
                if r["fill"] and not r["stroke"] and _is_white(r["non_stroking_color"])
                and r["x0"] <= word["x0"] + 0.5 and r["x1"] >= word["x1"] - 0.5
                and r["top"] <= word["top"] + 0.5 and r["bottom"] >= word["bottom"] - 0.5
            ]
            assert len(backing) == 1, (page.page_number, backing)
            rect = backing[0]
            # Tight: about the padding around the glyph box, not a big white patch.
            assert word["x0"] - rect["x0"] <= pad + 1.5 and rect["x1"] - word["x1"] <= pad + 1.5
            assert rect["bottom"] - rect["top"] <= word["bottom"] - word["top"] + 2 * pad + 4
            # The stamp form draws it after the page content, so it is on top of the image.
            assert page.images, page.page_number


def _is_white(color) -> bool:
    if isinstance(color, (int, float)):
        return float(color) == 1.0
    return all(float(v) == 1.0 for v in color)


def test_stamp_is_backed_by_white_rectangle_on_dark_pages(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    m = make_dark_pdf(d / "dark.pdf", [LETTER, LANDSCAPE, A4_ROT90, A4_ROT270, LETTER_ROT180])
    p = plan([bundle(1, m, 5)], options())
    (volume,) = write_volumes(p, [bundle(1, m, 5)], None, options(), tmp_path / "out")
    assert_stamps_bottom_right(volume.path)
    assert_stamps_have_white_backing(volume.path)
    # The backing is inside the stamp form, not painted onto the page content.
    reader = PdfReader(str(volume.path))
    for page in reader.pages:
        form = page["/Resources"]["/XObject"][f"/{assemble.STAMP_XOBJECT}"].get_object()
        assert b" re\n" in form.get_data() or b" re " in form.get_data()
        assert b"1 g" in form.get_data()


def test_stamp_backing_also_present_on_plain_pages(tmp_path):
    p, bundles, index, opts = build_scenario(tmp_path)
    volumes = write_volumes(p, bundles, index, opts, tmp_path / "out")
    for volume in volumes:
        with pdfplumber.open(str(volume.path)) as pdf:
            for page in pdf.pages:
                (word,) = [w for w in page.extract_words() if w["text"].startswith("DOC-")]
                assert any(
                    r["fill"] and _is_white(r["non_stroking_color"])
                    and r["x0"] <= word["x0"] + 0.5 and r["x1"] >= word["x1"] - 0.5
                    for r in page.rects
                ), page.page_number


def test_append_pdf_strips_page_level_channels(tmp_path):
    """Pages merged into a volume lose the same associated files, XMP,
    PieceInfo and thumbnails as the attachment sanitizer drops. (The page
    allowlist for unknown vendor keys is the sanitizer's alone: everything
    reaching the assembler was produced by pst2pdf or already sanitized.)"""
    from test_convert_attachments import EXCLUDED_CHANNEL_KEYS, assert_no_page_channels, channel_pdf_bytes

    d = tmp_path / "in"
    d.mkdir()
    m = d / "apple.pdf"
    m.write_bytes(channel_pdf_bytes())
    for key in EXCLUDED_CHANNEL_KEYS:
        assert key in PdfReader(str(m)).pages[0], key
    p = plan([bundle(1, m, 1)], options())
    (volume,) = write_volumes(p, [bundle(1, m, 1)], None, options(), tmp_path / "out")
    assert_no_page_channels(volume.path, keys=EXCLUDED_CHANNEL_KEYS)
    page = PdfReader(str(volume.path)).pages[0]
    assert "visible text" in page.extract_text() and "DOC-000001" in page.extract_text()


def test_stamp_form_leaves_original_content_untouched(tmp_path):
    p, bundles, index, opts = build_scenario(tmp_path)
    volumes = write_volumes(p, bundles, index, opts, tmp_path / "out")
    reader = PdfReader(str(volumes[0].path))
    src = PdfReader(str(bundles[0].message_pdf))
    page = reader.pages[1]
    contents = page["/Contents"]
    assert isinstance(contents, list) and len(contents) == 4  # q, original, Q, Do
    assert contents[1].get_object().get_data() == src.pages[0]["/Contents"].get_object().get_data()
    form = page["/Resources"]["/XObject"][f"/{assemble.STAMP_XOBJECT}"].get_object()
    assert form["/Subtype"] == "/Form"
    assert "/Font" in form["/Resources"]


def test_stamp_with_resources_shared_between_pages(tmp_path):
    """Pages that share one indirect /Resources dict must not share a stamp."""
    d = tmp_path / "in"
    d.mkdir()
    m = make_pdf(d / "m.pdf", [LETTER, LETTER, LANDSCAPE])
    reader = PdfReader(str(m))
    writer = PdfWriter()
    shared = None
    for page in reader.pages:
        page = writer.add_page(page)
        if shared is None:
            shared = writer._add_object(page["/Resources"])
        page[NameObject("/Resources")] = shared
    with open(m, "wb") as fh:
        writer.write(fh)
    check = PdfReader(str(m))
    assert len({check.pages[i].raw_get("/Resources").idnum for i in range(3)}) == 1

    p = plan([bundle(1, m, 3)], options())
    (volume,) = write_volumes(p, [bundle(1, m, 3)], None, options(), tmp_path / "out")
    out = PdfReader(str(volume.path))
    for page, label in zip(out.pages, ("DOC-000001", "DOC-000002", "DOC-000003")):
        assert re.findall(r"DOC-\d{6}", page.extract_text()) == [label]
    forms = {
        out.pages[i]["/Resources"]["/XObject"].raw_get(f"/{assemble.STAMP_XOBJECT}").idnum
        for i in range(3)
    }
    assert len(forms) == 3
    assert_stamps_bottom_right(volume.path)


def test_write_volumes_is_deterministic(tmp_path):
    p, bundles, index, opts = build_scenario(tmp_path)
    first = write_volumes(p, bundles, index, opts, tmp_path / "a")
    second = write_volumes(p, bundles, index, opts, tmp_path / "b")
    for va, vb in zip(first, second):
        assert va.path.read_bytes() == vb.path.read_bytes()


def test_write_volumes_no_front_matter(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    m = make_pdf(d / "m.pdf", [LETTER])
    opts = options(bates=BatesSpec(prefix="X-", start=41, width=4))
    p = plan([bundle(1, m, 1)], opts)
    (volume,) = write_volumes(p, [bundle(1, m, 1)], None, opts, tmp_path / "out")
    reader = PdfReader(str(volume.path))
    assert reader.page_labels == ["X-0041"]
    assert outline_titles(reader.outline) == ["Volume 01 of 01", ["0001 · 2024-03-15 09:12"]]


def test_write_volumes_page_count_mismatch_raises(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    m = make_pdf(d / "m.pdf", [LETTER])
    a = make_pdf(d / "a.pdf", [LETTER])
    b = bundle(1, m, 1, [entry(1, 1, converted(a, 3))])  # claims 3 pages, file has 1
    p = plan([b], options())
    with pytest.raises(AssembleError, match="attachment 1"):
        write_volumes(p, [b], None, options(), tmp_path / "out")


def test_write_volumes_missing_crossref_page_raises(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    m = make_pdf(d / "m.pdf", [LETTER])
    b = bundle(1, m, 1, [entry(1, 1, None, duplicate_of=1)])
    p = plan([b], options())
    with pytest.raises(AssembleError, match="cross-reference"):
        write_volumes(p, [b], None, options(), tmp_path / "out")


def test_write_volumes_missing_index_raises(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    m = make_pdf(d / "m.pdf", [LETTER])
    p = plan([bundle(1, m, 1)], options(), front_matter_pages=1)
    with pytest.raises(AssembleError, match="index"):
        write_volumes(p, [bundle(1, m, 1)], None, options(), tmp_path / "out")


# --------------------------------------------------------------------------- #
def test_no_bates_leaves_the_pages_unnumbered(tmp_path):
    """--no-bates: nothing drawn, no page labels, everything else unchanged."""
    p, bundles, index, _ = build_scenario(tmp_path)
    opts = options(volume_pages=9, stamp_bates=False)
    volumes = write_volumes(p, bundles, index, opts, tmp_path / "out")

    assert [v.page_count for v in volumes] == [9, 3]
    for volume in volumes:
        reader = PdfReader(str(volume.path))
        # No /PageLabels at all, so Acrobat shows each volume's own 1..n.
        assert "/PageLabels" not in reader.trailer["/Root"]
        assert reader.page_labels == [str(i) for i in range(1, len(reader.pages) + 1)]
        for page in reader.pages:
            assert not re.findall(r"DOC-\d{6}", page.extract_text())
            assert f"/{assemble.STAMP_XOBJECT}" not in page.get("/Resources", {}).get("/XObject", {})
    # The plan still numbers the pages; only the printing is suppressed.
    assert [v.bates for v in p.volumes] == [PageRange(1, 9), PageRange(10, 12)]
    # Bookmarks and metadata are untouched.
    v1 = PdfReader(str(volumes[0].path))
    assert outline_titles(v1.outline)[0] == "Volume 01 of 02"
    assert v1.metadata.title == "REQ-2026-001 — Volume 01 of 02"


# --------------------------------------------------------------------------- #
# Stamp font
# --------------------------------------------------------------------------- #


def test_stamp_font_falls_back_to_helvetica(monkeypatch):
    monkeypatch.setattr(assemble, "_stamp_font", None)
    monkeypatch.setattr(assemble, "FONT_CANDIDATES", (Path("/nonexistent/DejaVuSans.ttf"),))
    assert assemble.stamp_font_name() == "Helvetica"


def test_stamp_font_registers_truetype_when_present(monkeypatch, tmp_path):
    # reportlab ships Bitstream Vera, DejaVu's ancestor; stand it in for DejaVu Sans.
    vera = Path(os.path.dirname(reportlab.__file__)) / "fonts" / "Vera.ttf"
    monkeypatch.setattr(assemble, "_stamp_font", None)
    monkeypatch.setattr(assemble, "FONT_CANDIDATES", (vera,))
    assert assemble.stamp_font_name() == assemble.STAMP_FONT_NAME

    d = tmp_path / "in"
    d.mkdir()
    m = make_pdf(d / "m.pdf", [LETTER, A4_ROT90])
    p = plan([bundle(1, m, 2)], options())
    (volume,) = write_volumes(p, [bundle(1, m, 2)], None, options(), tmp_path / "out")
    reader = PdfReader(str(volume.path))
    for page, label in zip(reader.pages, ("DOC-000001", "DOC-000002")):
        assert label in page.extract_text()  # subset font carries a ToUnicode map
        fonts = page["/Resources"]["/XObject"][f"/{assemble.STAMP_XOBJECT}"]["/Resources"]["/Font"]
        descriptors = [f.get_object().get("/FontDescriptor") for f in fonts.values()]
        assert len(descriptors) == 1  # the stamp font and nothing else
        assert "/FontFile2" in descriptors[0].get_object()  # embedded TrueType subset
    assert_stamps_bottom_right(volume.path)


# --------------------------------------------------------------------------- #
# Appendix volumes (--appendix-over)
# --------------------------------------------------------------------------- #


def pointer(index: int, total: int, conv, item: int, separator=None) -> AttachmentEntry:
    """A slot whose pages were moved to appendix item *item*."""
    e = entry(index, total, conv, separator=separator)
    e.appendix_item = item
    return e


def appendix_item(number: int, conv, carriers, header=None, filename="Secret log of Jane Doe.txt") -> AppendixItem:
    return AppendixItem(
        number=number,
        filename=filename,
        size=7,
        content_type="text/plain",
        sha256=f"{number:064d}",
        converted=conv,
        carriers=list(carriers),
        header_pdf=header,
    )


def build_appendix_world(tmp_path: Path):
    """Three messages; message 1 carries a long attachment (item 1) and an
    inline one, message 2 carries item 1 again (dedup) and item 2, message 3
    carries nothing."""
    d = tmp_path / "appendix-in"
    d.mkdir(exist_ok=True)
    pdfs = {
        "one": make_pdf(d / "one.pdf", [LETTER]),
        "two": make_pdf(d / "two.pdf", [LETTER, LETTER]),
        "header": make_pdf(d / "header.pdf", [LETTER]),
        "long": make_pdf(d / "long.pdf", [LETTER] * 6),
        "longer": make_pdf(d / "longer.pdf", [LANDSCAPE, A4_ROT90, LETTER, LETTER]),
    }
    long_conv = converted(pdfs["long"], 6)
    longer_conv = converted(pdfs["longer"], 4, "passthrough")
    items = [
        appendix_item(1, long_conv, [(1, 1), (2, 1)], header=pdfs["header"]),
        appendix_item(2, longer_conv, [(2, 2)], header=pdfs["header"]),
    ]
    bundles = [
        bundle(1, pdfs["two"], 2, [pointer(1, 2, long_conv, 1, pdfs["one"]), entry(2, 2, converted(pdfs["two"], 2))]),
        bundle(2, pdfs["one"], 1, [pointer(1, 2, long_conv, 1, pdfs["one"]), pointer(2, 2, longer_conv, 2, pdfs["one"])],
               date=datetime(2024, 4, 1, 17, 5)),
        bundle(3, pdfs["one"], 1),
    ]
    return bundles, items, pdfs


@pytest.fixture
def appendix_world(tmp_path):
    return build_appendix_world(tmp_path)


def test_plan_appendix_continues_bates_after_the_message_volumes(appendix_world):
    bundles, items, _ = appendix_world
    p = plan(bundles, options(), front_matter_pages=2, appendix=items)

    # Message stream: every pointer slot is one page, whatever the attachment's length.
    assert p.message_ranges == {1: PageRange(3, 4), 2: PageRange(8, 8), 3: PageRange(11, 11)}
    assert p.attachment_ranges == {
        (1, 1): PageRange(5, 5),  # pointer
        (1, 2): PageRange(6, 7),  # inline, two pages
        (2, 1): PageRange(9, 9),  # pointer to the same item
        (2, 2): PageRange(10, 10),
    }
    assert [v.bates for v in p.volumes] == [PageRange(1, 11)]
    # Appendix: header sheet + content, numbered straight on from page 11.
    assert p.appendix_ranges == {1: PageRange(12, 18), 2: PageRange(19, 23)}
    (av,) = p.appendix_volumes
    assert av.path == Path("Attachments 01 of 01.pdf")
    assert (av.number, av.total, av.first_seq, av.last_seq) == (1, 1, 1, 2)
    assert av.bates == PageRange(12, 23) and av.page_count == 12


def test_plan_appendix_splits_like_message_volumes(appendix_world):
    bundles, items, _ = appendix_world
    # Items are 7 and 5 pages: at 8 pages per volume each gets its own.
    p = plan(bundles, options(volume_pages=8), appendix=items)
    assert [(v.first_seq, v.last_seq) for v in p.appendix_volumes] == [(1, 1), (2, 2)]
    assert [v.path.name for v in p.appendix_volumes] == ["Attachments 01 of 02.pdf", "Attachments 02 of 02.pdf"]
    assert all(v.total == 2 for v in p.appendix_volumes)
    assert p.appendix_volumes[0].bates.start == p.volumes[-1].bates.end + 1
    assert p.appendix_volumes[1].bates.start == p.appendix_volumes[0].bates.end + 1
    # An exact fit shares a volume; an oversized item still gets one of its own.
    p = plan(bundles, options(volume_pages=12), appendix=items)
    assert [(v.first_seq, v.last_seq) for v in p.appendix_volumes] == [(1, 2)]
    p = plan(bundles, options(volume_pages=3), appendix=items)
    assert [v.page_count for v in p.appendix_volumes] == [7, 5]


def test_plan_appendix_counts_unrendered_header_as_one_page(appendix_world):
    bundles, items, pdfs = appendix_world
    items[0].header_pdf = None
    items[1].converted = converted(pdfs["longer"], 0, "passthrough")  # page_count unfilled: read the file
    p = plan(bundles, options(), appendix=items)
    first = p.volumes[-1].bates.end + 1
    assert p.appendix_ranges == {1: PageRange(first, first + 6), 2: PageRange(first + 7, first + 11)}


def test_plan_without_appendix_is_unchanged(pdfs):
    """Option off: no appendix argument, or an empty one, gives the same plan
    as before the feature, field for field."""
    bundles = [
        bundle(1, pdfs["two"], 2, [entry(1, 2, converted(pdfs["three"], 3), separator=pdfs["one"]),
                                  entry(2, 2, None, separator=pdfs["one"], duplicate_of=1)]),
        bundle(2, pdfs["three"], 3),
    ]
    base = plan(bundles, options(volume_pages=5), front_matter_pages=1)
    assert plan(bundles, options(volume_pages=5), front_matter_pages=1, appendix=[]) == base
    assert base.appendix_volumes == [] and base.appendix_ranges == {}


def test_write_appendix_end_to_end(appendix_world, tmp_path):
    bundles, items, pdfs = appendix_world
    opts = options(volume_pages=8)
    p = plan(bundles, opts, front_matter_pages=1, appendix=items)
    out = tmp_path / "out"
    volumes = write_volumes(p, bundles, pdfs["one"], opts, out)
    appendix = assemble.write_appendix(p, items, bundles, opts, out)

    assert [v.path.name for v in appendix] == ["Attachments 01 of 02.pdf", "Attachments 02 of 02.pdf"]
    assert [v.page_count for v in appendix] == [7, 5]
    # One Bates sequence across the message volumes and then the appendix.
    labels = []
    for v in [*volumes, *appendix]:
        reader = PdfReader(str(v.path))
        assert len(reader.pages) == v.page_count
        for page, label in zip(reader.pages, reader.page_labels):
            assert re.findall(r"DOC-\d{6}", page.extract_text()) == [label]
        labels.extend(reader.page_labels)
    assert labels == [f"DOC-{n:06d}" for n in range(1, len(labels) + 1)]
    for v in appendix:
        assert_stamps_bottom_right(v.path)

    a1, a2 = (PdfReader(str(v.path)) for v in appendix)
    assert a1.metadata.title == "REQ-2026-001 — Attachments 01 of 02"
    assert a1.metadata.producer == f"pst2pdf {__version__}"
    for key in ("/Author", "/Subject", "/Keywords", "/CreationDate", "/ModDate", "/Creator"):
        assert key not in a1.metadata
    # Outline: item number and the first carrying message (seq, date), never the filename.
    assert outline_titles(a1.outline) == ["Attachments 01 of 02", ["Item 1 · message 0001 · 2024-03-15 09:12"]]
    assert outline_titles(a2.outline) == ["Attachments 02 of 02", ["Item 2 · message 0002 · 2024-04-01 17:05"]]
    assert a1.get_destination_page_number(a1.outline[1][0]) == 0
    for reader in (a1, a2):
        titles = str(outline_titles(reader.outline))
        assert "Jane" not in titles and ".txt" not in titles and SECRET not in titles

    # The message volume carries the one-page pointers, bookmarked as attachments.
    v1 = PdfReader(str(volumes[0].path))
    assert outline_titles(v1.outline)[1][2] == ["Attachment 1", "Attachment 2"]


def test_write_appendix_without_bates(appendix_world, tmp_path):
    bundles, items, _ = appendix_world
    opts = options(stamp_bates=False)
    p = plan(bundles, opts, appendix=items)
    (volume,) = assemble.write_appendix(p, items, bundles, opts, tmp_path / "out")
    reader = PdfReader(str(volume.path))
    assert "/PageLabels" not in reader.trailer["/Root"]
    for page in reader.pages:
        assert not re.findall(r"DOC-\d{6}", page.extract_text())


def test_write_appendix_raises_on_drift_or_missing_pages(tmp_path):
    bundles, items, pdfs = build_appendix_world(tmp_path)
    items[1].converted = converted(pdfs["longer"], 9, "passthrough")  # claims 9 pages, file has 4
    p = plan(bundles, options(), appendix=items)
    with pytest.raises(AssembleError, match="appendix item 2"):
        assemble.write_appendix(p, items, bundles, options(), tmp_path / "a")

    bundles, items, pdfs = build_appendix_world(tmp_path)
    items[0].header_pdf = None
    p = plan(bundles, options(), appendix=items)
    with pytest.raises(AssembleError, match="header sheet"):
        assemble.write_appendix(p, items, bundles, options(), tmp_path / "b")


def test_write_volumes_missing_pointer_page_raises(appendix_world, tmp_path):
    bundles, items, _ = appendix_world
    bundles[0].attachments[0].separator_pdf = None
    p = plan(bundles, options(), appendix=items)
    with pytest.raises(AssembleError, match="appendix pointer"):
        write_volumes(p, bundles, None, options(), tmp_path / "out")
