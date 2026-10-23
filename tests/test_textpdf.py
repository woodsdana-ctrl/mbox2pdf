"""textpdf: plain-text attachments straight to PDF with reportlab."""

from __future__ import annotations

import pdfplumber
from pypdf import PdfReader

from pst2pdf import textpdf


def _render(tmp_path, text: str):
    out = tmp_path / "t.pdf"
    pages = textpdf.render_text_pdf(textpdf.normalize(text), out)
    return pages, PdfReader(str(out))


def test_normalize_line_endings_tabs_and_controls():
    assert textpdf.normalize("a\r\nb\rc\fd") == "a\nb\nc\nd"
    assert textpdf.normalize("x\ty") == "x       y"
    assert textpdf.normalize("nul\x00gone") == "nulgone"
    assert textpdf.normalize("esc\x1b[0m") == "esc�[0m"


def test_wrap_prefers_spaces_and_breaks_long_runs():
    assert textpdf.wrap("short\n\nlines\n", 10) == ["short", "", "lines"]
    assert textpdf.wrap("aaaa bbbb cccc", 10) == ["aaaa bbbb ", "cccc"]
    assert textpdf.wrap("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
    assert textpdf.wrap("", 10) == [""]


def test_lines_per_page_matches_the_old_weasyprint_page():
    assert textpdf.LINES_PER_PAGE == 59


def test_page_count_and_empty_text(tmp_path):
    pages, reader = _render(tmp_path, "".join(f"line {i}\n" for i in range(59)))
    assert pages == len(reader.pages) == 1
    pages, reader = _render(tmp_path, "".join(f"line {i}\n" for i in range(60)))
    assert pages == len(reader.pages) == 2
    pages, reader = _render(tmp_path, "")
    assert pages == len(reader.pages) == 1


def test_wrapped_lines_count_toward_the_page(tmp_path):
    font, _ = textpdf._load_font()
    columns = textpdf._columns(font)
    long_line = " ".join(["word"] * (columns // 5 * 3))  # wraps onto about three lines
    pages, reader = _render(tmp_path, "\n".join([long_line] * 30))
    assert pages == 2


def test_words_are_extractable_for_search_and_premark(tmp_path):
    _, reader = _render(tmp_path, "Jane Doe called 612-555-0100\nsecond line")
    with pdfplumber.open(reader.stream) as pdf:
        words = [w["text"] for w in pdf.pages[0].extract_words()]
    assert words[:4] == ["Jane", "Doe", "called", "612-555-0100"]
    assert "second" in words


def test_metadata_names_nothing(tmp_path):
    _, reader = _render(tmp_path, "x")
    meta = reader.metadata or {}
    assert not meta.get("/Title") and not meta.get("/Author")


def test_covers():
    assert textpdf.covers("plain ASCII log line\n")
    assert not textpdf.covers("\U0001f600")  # no emoji in either font


# --- delimited text as a table ---------------------------------------------------

HEADER = "EmpID\tLastname\tFirstname\tLanguageCode\tJobTitle\tEmailAddress"


def _tsv(rows: int, last="DELGADO") -> str:
    lines = [HEADER]
    for i in range(rows):
        lines.append(f"{10000001 + i}\t{last}\tMARIA\t\tAnalyst, Budget\tMARIA.{last}@EXAMPLE.ORG")
    return "\r\n".join(lines) + "\r\n"


def _page_words(path):
    with pdfplumber.open(path) as pdf:
        return [[w["text"] for w in page.extract_words()] for page in pdf.pages]


def test_table_delimiter():
    assert textpdf.table_delimiter(_tsv(3)) == "\t"
    csv_text = "id,name\n1,Jane Doe\n2,\"Roe, Richard\"\n"
    assert textpdf.table_delimiter(csv_text) is None  # commas only when asked
    assert textpdf.table_delimiter(csv_text, allow_comma=True) == ","
    prose = "Dear Jane, thanks for the note.\nWe met on Tuesday, as planned.\nBest, Pat\n"
    assert textpdf.table_delimiter(prose, allow_comma=False) is None
    assert textpdf.table_delimiter("a\tb\n") is None  # one line is not a table


def test_parse_table_cleans_cells_and_pads_rows():
    rows = textpdf.parse_table('id,name,note\n1,"Doe,\n Jane",x\n2,Roe\n\n', ",")
    assert rows == [["id", "name", "note"], ["1", "Doe, Jane", "x"], ["2", "Roe", ""]]


def test_looks_like_header():
    assert textpdf.looks_like_header(HEADER.split("\t"))
    assert not textpdf.looks_like_header(["10000001", "DELGADO", "MARIA"])  # a number
    assert not textpdf.looks_like_header(["DELGADO", "MARIA", "MARIA@EXAMPLE.ORG"])  # an address
    assert not textpdf.looks_like_header(["DELGADO", "", "", ""])  # mostly empty


def test_table_repeats_the_header_and_drops_empty_columns(tmp_path):
    out = tmp_path / "t.pdf"
    pages = textpdf.render_table_pdf(_tsv(120), "\t", out)
    words = _page_words(out)
    assert pages == len(words) >= 2
    for page in words:
        assert page[:2] == ["EmpID", "Lastname"] or page[:2] == ["1", "column"]
    assert words[0][:7] == ["1", "column", "with", "no", "values", "not", "shown:"]
    assert "LanguageCode" not in [w for page in words[1:] for w in page]
    assert "MARIA.DELGADO@EXAMPLE.ORG" in words[0]
    # Every data row printed once, on one line each.
    assert sum(page.count("MARIA") for page in words) == 120


def test_table_without_a_header_repeats_nothing(tmp_path):
    text = "\n".join(f"{1000 + i}\tDOE\tJANE\tjane{i}@example.org" for i in range(150))
    out = tmp_path / "t.pdf"
    textpdf.render_table_pdf(text, "\t", out)
    words = _page_words(out)
    assert len(words) >= 2
    assert words[1][0] != "1000"  # the first data row is not a header
    assert sum(page.count("1000") for page in words) == 1


def test_table_never_splits_a_word(tmp_path):
    """Columns shrink to their longest word, never below: a long hyphenated
    surname in a narrow, wrapped table stays whole for search and premark."""
    header = "\t".join(f"Column {c}" for c in range(9))
    row = "\t".join(["Garcia-Fernandez"] + ["words that can wrap at the spaces between them"] * 8)
    out = tmp_path / "t.pdf"
    textpdf.render_table_pdf(header + "\n" + "\n".join([row] * 20), "\t", out)
    words = [w for page in _page_words(out) for w in page]
    assert words.count("Garcia-Fernandez") == 20


def test_table_too_wide_to_keep_words_falls_back_to_plain_text(tmp_path):
    header = "\t".join(f"C{c}" for c in range(12))
    row = "\t".join(["x" * 40] * 12)  # twelve unbreakable 40-character words
    out = tmp_path / "t.pdf"
    textpdf.render_table_pdf(header + "\n" + row + "\n" + row, "\t", out)
    page = PdfReader(str(out)).pages[0]
    assert [float(v) for v in page.mediabox] == [0, 0, 612, 792]  # plain text is portrait
    assert "C0      C1" in page.extract_text()  # tabs expanded, not a table


def test_wide_table_turns_landscape_when_that_saves_pages(tmp_path):
    """Portrait would wrap every row onto two lines; landscape keeps them on
    one, so far fewer pages. (With ten rows both fit one page, and portrait's
    larger font stays.)"""
    header = "\t".join(["Name", "Title", "Email", "Department", "Building"])
    row = "\t".join(["Patricia Delgado", "Analyst, Program Operations", "patricia.delgado@example.org", "Finance and Administration", "North Office"])
    out = tmp_path / "t.pdf"
    pages = textpdf.render_table_pdf(header + "\n" + "\n".join([row] * 200), "\t", out)
    page = PdfReader(str(out)).pages[0]
    assert [float(v) for v in page.mediabox] == [0, 0, 792, 612]
    assert pages == 4  # 200 one-line rows at 7 pt, 51 under the header: 20% fewer than 8.5 pt
    ten = tmp_path / "ten.pdf"
    textpdf.render_table_pdf(header + "\n" + "\n".join([row] * 10), "\t", ten)
    assert [float(v) for v in PdfReader(str(ten)).pages[0].mediabox] == [0, 0, 612, 792]
