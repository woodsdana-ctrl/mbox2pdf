"""Tests for premark.py: term lists, patterns, and /Redact annotation placement.

Fixture PDFs are drawn with reportlab so every word's true position is known
independently of pdfplumber. Helvetica 12 pt: pdfminer's glyph box runs from
baseline + descent (-2.484) to baseline + descent + size, ink reaches
baseline + 8.6 (ascender 718/1000).
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from types import SimpleNamespace

import pdfplumber
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from pst2pdf import premark
from pst2pdf.premark import PATTERNS, Term, apply, load_terms, resolve_patterns

FONT, SIZE = "Helvetica", 12
DESCENT = -0.207 * SIZE
ASCENT = 0.718 * SIZE


def draw_pdf(path: Path, pages, rotate=0, mediabox=None) -> Path:
    """pages: list of (pagesize, [(x, y, text), ...]). Same content on every page."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=pages[0][0])
    for pagesize, lines in pages:
        c.setPageSize(pagesize)
        c.setFont(FONT, SIZE)
        for x, y, text in lines:
            c.drawString(x, y, text)
        c.showPage()
    c.save()
    reader = PdfReader(io.BytesIO(buf.getvalue()))
    writer = PdfWriter()
    for page in reader.pages:
        added = writer.add_page(page)
        if rotate:
            added.rotate(rotate)
        if mediabox:
            added.mediabox = RectangleObject(mediabox)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


def expected_box(x, y, text, phrase):
    """User-space box of ``phrase`` inside a line drawn at (x, y)."""
    i = text.index(phrase)
    x0 = x + stringWidth(text[:i], FONT, SIZE)
    x1 = x + stringWidth(text[: i + len(phrase)], FONT, SIZE)
    return x0, y + DESCENT, x1, y + ASCENT


def assert_encloses(rect, box, slack=0.6, excess=4.0):
    rx0, ry0, rx1, ry1 = (float(v) for v in rect)
    bx0, by0, bx1, by1 = box
    assert rx0 <= bx0 + slack and rx1 >= bx1 - slack, (rect, box)
    assert ry0 <= by0 + slack and ry1 >= by1 - slack, (rect, box)
    # ... and is not wildly bigger than the words it marks.
    assert rx1 - rx0 <= (bx1 - bx0) + excess and ry1 - ry0 <= (by1 - by0) + excess + SIZE, (rect, box)


def annots(path: Path):
    """[(page_index, rotation, annot dict)] for every annotation in the file."""
    out = []
    for i, page in enumerate(PdfReader(path).pages):
        for ref in page.get("/Annots", []):
            out.append((i, page.rotation, ref.get_object()))
    return out


def contents(path: Path):
    return [p.get_contents().get_data() for p in PdfReader(path).pages]


# ---------------------------------------------------------------------------
# Term lists and variants


def test_load_terms(tmp_path):
    f = tmp_path / "terms.txt"
    f.write_text(
        "# Request 2026-0001\n"
        "\n"
        "[student]  Jane Doe +variants\n"
        "[student]  Doe, Jane\n"
        "[parent]   Robert Doe +variants +first\n"
        "Plain Term\n"
        "[]  Empty Category\n"
        "[pattern]  Phone\n",
        encoding="utf-8",
    )
    terms = load_terms(f)
    assert terms == [
        Term("Jane Doe", "student", variants=True),
        Term("Doe, Jane", "student"),
        Term("Robert Doe", "parent", variants=True, first=True),
        Term("Plain Term", None),
        Term("Empty Category", None),
        Term("phone", "pattern"),
    ]
    assert terms[-1].is_pattern


@pytest.mark.parametrize(
    "bad",
    ["[student] Jane Doe +middle", "[student]", "[pattern] nosuchpattern"],
)
def test_load_terms_rejects_bad_lines(tmp_path, bad):
    f = tmp_path / "terms.txt"
    f.write_text(bad + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_terms(f)


@pytest.mark.parametrize(
    "line",
    [
        "[Doe] Jane Doe",  # the category is the name it would cover
        "[doe] Jane Doe",  # case does not matter
        "[Jane] Doe, Jane +variants",  # appears in a variant
        "[student] Student Council",  # a category inside an ordinary term is still stamped over it
    ],
)
def test_load_terms_rejects_category_inside_term(tmp_path, line):
    f = tmp_path / "terms.txt"
    f.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="printed over the redaction"):
        load_terms(f)


def test_load_terms_allows_category_that_is_not_in_the_term(tmp_path):
    f = tmp_path / "terms.txt"
    f.write_text("[student] Jane Doe +variants +first\n[pattern] phone\n", encoding="utf-8")
    assert [t.text for t in load_terms(f)] == ["Jane Doe", "phone"]


def test_variants():
    assert Term("Jane Doe").phrases() == ["Jane Doe"]
    assert Term("Jane Doe", variants=True).phrases() == ["Jane Doe", "Doe, Jane", "J. Doe", "Doe"]
    assert Term("Jane Doe", variants=True, first=True).phrases() == [
        "Jane Doe", "Doe, Jane", "J. Doe", "Doe", "Jane",
    ]
    assert Term("Jane Doe", first=True).phrases() == ["Jane Doe", "Jane"]
    # "Last, First" input, three-part names, and single words.
    assert Term("Doe, Jane", variants=True).phrases() == ["Doe, Jane", "Jane Doe", "J. Doe", "Doe"]
    assert Term("Mary Ann Smith", variants=True).phrases() == [
        "Mary Ann Smith", "Smith, Mary Ann", "M. Smith", "Smith",
    ]
    assert Term("Cher", variants=True, first=True).phrases() == ["Cher"]
    # An initial already in the term does not duplicate the "F. Last" form.
    assert Term("J. Doe", variants=True).phrases() == ["J. Doe", "Doe, J.", "Doe"]


# ---------------------------------------------------------------------------
# Patterns


@pytest.mark.parametrize(
    "text",
    ["612-555-0100", "(612) 555-0100", "612.555.0100", "612 555 0100", "+1 612-555-0100", "1-612-555-0100"],
)
def test_phone_pattern_matches(text):
    assert PATTERNS["phone"].search(f"call {text} today")


@pytest.mark.parametrize("text", ["DOC-000412", "6125550100", "555-0100", "2026-09-17", "612-555-01000"])
def test_phone_pattern_rejects(text):
    assert not PATTERNS["phone"].search(text)


def test_ssn_pattern():
    assert PATTERNS["ssn"].search("SSN 123-45-6789.")
    assert PATTERNS["ssn"].search("123 45 6789")
    assert not PATTERNS["ssn"].search("123456789")
    assert not PATTERNS["ssn"].search("612-555-0100")


@pytest.mark.parametrize(
    "text,date",
    [
        ("DOB: 01/02/2015", "01/02/2015"),
        ("D.O.B. 1/2/15", "1/2/15"),
        ("date of birth 2015-01-02", "2015-01-02"),
        ("born on January 2, 2015", "January 2, 2015"),
        ("Birthdate - 2 Jan 2015", "2 Jan 2015"),
    ],
)
def test_dob_pattern_marks_only_the_date(text, date):
    m = PATTERNS["dob"].search(text)
    assert m and m.group("hit") == date


def test_dob_pattern_needs_a_lead_in():
    assert not PATTERNS["dob"].search("meeting on 01/02/2015")


def test_resolve_patterns():
    assert set(resolve_patterns(["phone", "SSN"])) == {"phone", "ssn"}
    with pytest.raises(ValueError):
        resolve_patterns(["student-id"])
    with pytest.raises(ValueError):
        resolve_patterns(["nope"])
    opts = SimpleNamespace(student_id_regex=r"\bS\d{6}\b", patterns=["student-id"])
    resolved = resolve_patterns(None, opts)
    assert resolved["student-id"].search("id S123456 here")
    assert resolve_patterns({"custom": r"x\d"})["custom"].search("X1")


# ---------------------------------------------------------------------------
# Annotation placement

LINES = [
    (72, 700, "Please contact Jane Doe, or her mother at 612-555-0100 today."),
    (72, 680, "JANE DOE met jane doe and Doeful Jones; J. Doe was late."),
    (72, 660, "Field trip form for Jane"),  # term wraps onto the next line: must not match
    (72, 640, "Doe returned unsigned. DOB: 01/02/2015."),
]
EXPECTED_TERM = [
    expected_box(*LINES[0], "Jane Doe"),
    expected_box(*LINES[1], "JANE DOE"),
    expected_box(*LINES[1], "jane doe"),
    expected_box(*LINES[1], "J. Doe"),
    expected_box(*LINES[3], "Doe"),  # last-name variant; the wrapped "Jane / Doe" phrase itself is not a hit
]
EXPECTED_PHONE = expected_box(*LINES[0], "612-555-0100")
EXPECTED_DOB = expected_box(*LINES[3], "01/02/2015")


def run_apply(path, options=None, terms=None, patterns=("phone", "dob")):
    terms = terms if terms is not None else [Term("Jane Doe", "student", variants=True)]
    return apply([path], terms, list(patterns), options)


def check_marks(path, expected_term=EXPECTED_TERM):
    """Rects enclose the true boxes, in page order, regardless of rotation."""
    marks = annots(path)
    by_cat = {}
    for _, _, a in marks:
        assert a["/Subtype"] == "/Redact"
        assert a["/Type"] == "/Annot"
        assert [float(v) for v in a["/IC"]] == [0, 0, 0]
        assert [float(v) for v in a["/C"]] == [1, 0, 0]
        assert a["/F"] == 4 and a["/T"] == "pst2pdf"
        assert len(a["/QuadPoints"]) == 8
        x0, y0, x1, y1 = (float(v) for v in a["/Rect"])
        assert [float(v) for v in a["/QuadPoints"]] == [x0, y1, x1, y1, x0, y0, x1, y0]
        by_cat.setdefault(a["/Contents"], []).append(a["/Rect"])
    assert len({a["/NM"] for _, _, a in marks}) == len(marks), "NM must be unique"

    def sort_key(r):
        return (-float(r[1]), float(r[0]))

    term_rects = sorted(by_cat["student"], key=sort_key)
    assert len(term_rects) == len(expected_term)
    for rect, box in zip(term_rects, sorted(expected_term, key=lambda b: (-b[1], b[0]))):
        assert_encloses(rect, box)
    (phone,) = by_cat["phone"]
    assert_encloses(phone, EXPECTED_PHONE)
    (dob,) = by_cat["dob"]
    assert_encloses(dob, EXPECTED_DOB)
    return marks


def check_coverage(cov, files=1, pages=1):
    assert cov.term_hits == {"Jane Doe": 5 * pages}
    assert cov.phrase_hits["Jane Doe"] == 3 * pages  # not the "Jane" / "Doe" pair split across lines
    assert cov.phrase_hits["J. Doe"] == 1 * pages
    assert cov.phrase_hits["Doe"] == 1 * pages  # masked inside "Jane Doe"/"J. Doe"; not "Doeful"
    assert cov.phrase_hits["Doe, Jane"] == 0
    assert cov.pattern_hits == {"phone": pages, "dob": pages}
    assert cov.terms_without_hits == []
    assert cov.files == files and cov.pages == pages
    assert cov.total == 7 * pages


def test_portrait_marks_and_coverage(tmp_path):
    path = draw_pdf(tmp_path / "p.pdf", [(letter, LINES)])
    before = contents(path)
    cov = run_apply(path, SimpleNamespace(overlay={"student": "FERPA"}))
    check_coverage(cov)
    marks = check_marks(path)
    assert contents(path) == before, "page content streams must be untouched"
    overlays = {a["/Contents"]: a["/OverlayText"] for _, _, a in marks}
    assert overlays == {"student": "FERPA", "phone": "phone", "dob": "dob"}

    # Cross-check against pdfplumber's own read of the words and annotations.
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        words = {(w["text"], int(w["top"])): w for w in page.extract_words()}
        jane, doe = words[("Jane", 82)], words[("Doe,", 82)]
        redacts = [a for a in page.annots if a["contents"] == "student"]
        hit = min(redacts, key=lambda a: abs(a["x0"] - jane["x0"]) + abs(a["top"] - jane["top"]))
        assert hit["x0"] <= jane["x0"] + 0.5 and hit["x1"] >= doe["x1"] - stringWidth(",", FONT, SIZE) - 0.5
        assert hit["top"] <= jane["top"] + 0.5 and hit["bottom"] >= jane["bottom"] - 0.5


def test_overlay_defaults(tmp_path):
    path = draw_pdf(tmp_path / "p.pdf", [(letter, LINES)])
    run_apply(path, None, terms=[Term("Jane Doe"), Term("mother", "parent")], patterns=())
    overlays = {a["/OverlayText"] for _, _, a in annots(path)}
    assert overlays == {"REDACTED", "parent"}
    assert premark.overlay_text(None) == "REDACTED"
    assert premark.overlay_text("x", SimpleNamespace(overlay={"x": "cite"})) == "cite"


def test_landscape(tmp_path):
    path = draw_pdf(tmp_path / "l.pdf", [(landscape(letter), LINES)])
    check_coverage(run_apply(path))
    check_marks(path)


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_rotated_pages_get_user_space_rects(tmp_path, rotate):
    path = draw_pdf(tmp_path / f"r{rotate}.pdf", [(letter, LINES)], rotate=rotate)
    before = contents(path)
    check_coverage(run_apply(path))
    # Same content stream as the portrait page, so the same user-space rects.
    marks = check_marks(path)
    assert all(rot == rotate for _, rot, _ in marks)
    assert contents(path) == before


def test_offset_mediabox(tmp_path):
    # Content drawn at user (72, 700) on a page whose MediaBox does not start at 0,0.
    line = (172, 900, "Jane Doe was here.")
    path = draw_pdf(tmp_path / "o.pdf", [(letter, [line])], mediabox=(100, 200, 712, 992))
    cov = run_apply(path, patterns=())
    assert cov.term_hits == {"Jane Doe": 1}
    (_, _, a), = annots(path)
    assert_encloses(a["/Rect"], expected_box(*line, "Jane Doe"))


def test_rotated_offset_mediabox(tmp_path):
    line = (172, 900, "Jane Doe was here.")
    path = draw_pdf(tmp_path / "ro.pdf", [(letter, [line])], rotate=90, mediabox=(100, 200, 712, 992))
    run_apply(path, patterns=())
    (_, _, a), = annots(path)
    assert_encloses(a["/Rect"], expected_box(*line, "Jane Doe"))


def test_multi_page_multi_file_and_zero_hit_terms(tmp_path):
    a = draw_pdf(tmp_path / "a.pdf", [(letter, LINES), (letter, LINES)])
    b = draw_pdf(tmp_path / "b.pdf", [(letter, [(72, 700, "Nothing to see.")])])
    b_before = b.read_bytes()
    terms = [Term("Jane Doe", "student", variants=True), Term("Nobody Here", "student")]
    cov = apply([a, b], terms, ["phone", "dob"])
    assert cov.term_hits == {"Jane Doe": 10, "Nobody Here": 0}
    assert cov.terms_without_hits == ["Nobody Here"]
    assert cov.pattern_hits == {"phone": 2, "dob": 2}
    assert cov.files == 2 and cov.pages == 3
    assert {i for i, _, _ in annots(a)} == {0, 1}
    assert b.read_bytes() == b_before, "a file with no hits is left byte-identical"


def test_pattern_lines_in_term_list_and_student_id(tmp_path):
    path = draw_pdf(tmp_path / "s.pdf", [(letter, [(72, 700, "Student S123456 phoned 612-555-0100.")])])
    terms = [Term("phone", "pattern"), Term("student-id", "pattern")]
    with pytest.raises(ValueError):
        apply([path], terms, [], SimpleNamespace())
    cov = apply([path], terms, [], SimpleNamespace(student_id_regex=r"\bS\d{6}\b", patterns=[]))
    assert cov.pattern_hits == {"phone": 1, "student-id": 1}
    assert cov.term_hits == {}
    rects = {a["/Contents"]: a["/Rect"] for _, _, a in annots(path)}
    assert_encloses(rects["student-id"], expected_box(72, 700, "Student S123456 phoned 612-555-0100.", "S123456"))


def test_columns_are_not_joined(tmp_path):
    # "Jane" and "Doe" on the same baseline but far apart (table columns) must not match.
    path = draw_pdf(tmp_path / "c.pdf", [(letter, [(72, 700, "Jane"), (400, 700, "Doe")])])
    cov = run_apply(path, patterns=())
    assert cov.phrase_hits["Jane Doe"] == 0
    assert cov.phrase_hits["Doe"] == 1  # the last-name variant still marks the lone "Doe"
    assert len(annots(path)) == 1
    cov = run_apply(path, terms=[Term("Jane Doe")], patterns=())
    assert cov.term_hits == {"Jane Doe": 0}


def test_hit_inside_another_terms_match_is_dropped(tmp_path):
    text = "Her father Robert Doe signed; Jane Doe did not."
    path = draw_pdf(tmp_path / "n.pdf", [(letter, [(72, 700, text)])])
    # Order in the list must not matter: the longer match wins the span.
    terms = [Term("Jane Doe", "student", variants=True), Term("Robert Doe", "parent")]
    cov = run_apply(path, terms=terms, patterns=())
    assert cov.term_hits == {"Jane Doe": 1, "Robert Doe": 1}
    assert cov.phrase_hits["Doe"] == 0
    marks = annots(path)
    assert sorted(a["/Contents"] for _, _, a in marks) == ["parent", "student"]
    rects = {a["/Contents"]: a["/Rect"] for _, _, a in marks}
    assert_encloses(rects["parent"], expected_box(72, 700, text, "Robert Doe"))
    assert_encloses(rects["student"], expected_box(72, 700, text, "Jane Doe"))


def test_no_matchers_is_a_no_op(tmp_path):
    path = draw_pdf(tmp_path / "n.pdf", [(letter, LINES)])
    before = path.read_bytes()
    cov = apply([path], [], [])
    assert cov.total == 0 and cov.pages == 1
    assert path.read_bytes() == before


def test_annotation_dictionary_shape(tmp_path):
    path = draw_pdf(tmp_path / "d.pdf", [(letter, LINES)])
    run_apply(path)
    _, _, a = annots(path)[0]
    assert a["/Subj"] == "Redact"
    assert re.fullmatch(r"pst2pdf-p\d{5}-\d{4}", a["/NM"])
    assert a["/P"].get_object()["/Type"] == "/Page"
    assert a["/DA"].startswith("/Helv")
    assert a["/AP"]["/N"]["/Subtype"] == "/Form"


# --- parallel scanning ----------------------------------------------------------


def test_parallel_scan_writes_the_same_bytes_as_serial(tmp_path, monkeypatch):
    """Pages are scanned in worker chunks but annotated in page order in the
    parent, so a parallel run writes byte-for-byte what a serial run does,
    across several files and chunk boundaries."""
    monkeypatch.setattr(premark, "_CHUNK_MIN", 2)
    pages = [(letter, LINES)] * 7 + [(landscape(letter), [(72, 500, "Nothing to see")])] + [(letter, LINES)] * 3
    runs = {}
    for label, jobs in (("serial", 1), ("parallel", 3)):
        paths = [draw_pdf(tmp_path / f"{label}-{n}.pdf", pages, rotate=90 if n else 0) for n in range(2)]
        cov = apply(
            paths, [Term("Jane Doe", "student", variants=True)], ["phone", "dob"], SimpleNamespace(jobs=jobs)
        )
        runs[label] = (cov, [p.read_bytes() for p in paths])
    (s_cov, s_bytes), (p_cov, p_bytes) = runs["serial"], runs["parallel"]
    assert premark._chunks(tmp_path / "parallel-0.pdf", 3) == [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 11)]
    assert p_bytes == s_bytes
    assert p_cov == s_cov
    assert p_cov.files == 2 and p_cov.pages == 22 and p_cov.total == 7 * 20


def test_a_line_mixing_fonts_with_different_descents_stays_one_line():
    """Boxes on one baseline whose bottoms spread wider than the tolerance
    (CJK low, an emoji high) are still one line, so a name is not cut."""
    def word(text, x0, y0):
        return premark._Word(text, (x0, y0, x0 + 10 * len(text), y0 + 12), [])

    words = [word("寫道：Jane", 170, 758.0), word("Doe", 237, 758.6), word("✅", 265, 761.4), word("Next", 62, 744.0)]
    lines = [[w.text for w in line] for line in premark._lines(words)]
    assert lines == [["寫道：Jane", "Doe", "✅"], ["Next"]]
