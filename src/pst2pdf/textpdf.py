"""Plain-text attachments straight to PDF with reportlab.

A log or ``.txt`` attachment used to be wrapped in ``<pre>`` and laid out by
WeasyPrint, a full CSS engine, to print lines of monospace text. This writes
the same page (Letter, 0.75 in margins, DejaVu Sans Mono 8.5 pt, 59 lines a
page, long lines wrapped at spaces and broken anywhere only when a single
run is longer than a line) directly, many times faster. The text is real
text with a ToUnicode map, so Acrobat's Search & Redact and premark find it.

Only text the font can show takes this path: :func:`covers` says whether
every character has a glyph, and the converter sends anything else to
WeasyPrint, whose font fallback can draw it. Nothing is ever drawn as an
empty box that a reviewer could not read.

:func:`render_text_pdf` is the unit of work handed to the converter's worker
processes, so it stays module-level with picklable arguments and result.
"""

from __future__ import annotations

import csv
import io
import re
import textwrap
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_SIZE = 8.5
LEADING = FONT_SIZE * 1.35  # the line-height the WeasyPrint <pre> used
MARGIN = 54.0  # 0.75 in
BODY_MARGIN = 6.0  # WeasyPrint's default 8 px body margin, inside the page margin
TAB_SIZE = 8
PAGE_WIDTH, PAGE_HEIGHT = letter
LINES_PER_PAGE = int((PAGE_HEIGHT - 2 * MARGIN) // LEADING)  # 59

MONO_FONT_NAME = "PST2PDF-Mono"
MONO_FONT_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSansMono.ttf"),
    Path("/usr/local/share/fonts/DejaVuSansMono.ttf"),
    Path("/Library/Fonts/DejaVuSansMono.ttf"),
    Path("~/Library/Fonts/DejaVuSansMono.ttf"),
)

_font: tuple[str, frozenset[int] | None] | None = None  # (name, covered code points; None = cp1252)
_font_lock = threading.Lock()  # converter threads call covers() at once


def _load_font() -> tuple[str, frozenset[int] | None]:
    """Register DejaVu Sans Mono once per process. Without it (a dev host),
    fall back to the built-in Courier, which covers what cp1252 encodes."""
    global _font
    with _font_lock:
        if _font is None:
            found: tuple[str, frozenset[int] | None] = ("Courier", None)
            for candidate in MONO_FONT_CANDIDATES:
                path = candidate.expanduser()
                if not path.is_file():
                    continue
                face = TTFont(MONO_FONT_NAME, str(path))
                pdfmetrics.registerFont(face)
                found = (MONO_FONT_NAME, frozenset(face.face.charToGlyph))
                break
            _font = found
    return _font


def _printable(ch: str) -> bool:
    return ch == "\n" or unicodedata.category(ch)[0] != "C"


def normalize(text: str) -> str:
    """Line endings to ``\\n``, form feeds to line breaks, tabs expanded, NULs
    dropped (as before), and any other control character shown as U+FFFD so a
    reviewer sees that something was there."""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = "\n".join(line.expandtabs(TAB_SIZE) for line in text.split("\n"))
    if any(not _printable(ch) for ch in set(text)):
        text = "".join(ch if _printable(ch) else "\ufffd" for ch in text)
    return text


def covers(text: str) -> bool:
    """Can the monospace font draw every character of *text* (normalized)?"""
    name, covered = _load_font()
    chars = set(text) - {"\n"}
    if covered is None:
        try:
            "".join(chars).encode("cp1252")
        except UnicodeEncodeError:
            return False
        return True
    return all(ord(ch) in covered for ch in chars)


def _columns(font: str) -> int:
    width = PAGE_WIDTH - 2 * (MARGIN + BODY_MARGIN)
    return int(width // pdfmetrics.stringWidth("0", font, FONT_SIZE))


def wrap(text: str, columns: int) -> list[str]:
    """Physical lines: each logical line, wrapped at spaces to *columns*
    (breaking a longer run anywhere), keeping its own spacing."""
    wrapper = textwrap.TextWrapper(
        width=columns,
        expand_tabs=False,
        replace_whitespace=False,
        drop_whitespace=False,
        break_on_hyphens=False,
        break_long_words=True,
    )
    lines: list[str] = []
    for line in text.split("\n"):
        if len(line) <= columns:
            lines.append(line)
        else:
            lines.extend(wrapper.wrap(line) or [""])
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()  # a trailing newline ends the last line; it does not start another
    return lines or [""]


def render_text_pdf(text: str, out_path: Path) -> int:
    """Write normalized *text* (see :func:`normalize`) to *out_path*; return
    the page count. Callers check :func:`covers` first."""
    font, _ = _load_font()
    lines = wrap(text, _columns(font))
    pages = [lines[i : i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)] or [[""]]
    return _write(out_path, pages, letter, FONT_SIZE)


def _write(out_path: Path, pages: list[list[str]], pagesize: tuple[float, float], size: float) -> int:
    """Draw each page's lines in the monospace font; return the page count."""
    font, _ = _load_font()
    leading = size * 1.35
    ascent = pdfmetrics.getFont(font).face.ascent / 1000 * size
    # First baseline where the CSS line box would put it: half the extra
    # leading above the glyphs, then the ascent. The body margin sits above
    # the first page's first line only, as in paged CSS.
    top = pagesize[1] - MARGIN - (leading - size) / 2 - ascent
    c = canvas.Canvas(str(out_path), pagesize=pagesize, invariant=1, pageCompression=1)
    c.setTitle("")
    c.setAuthor("")
    c.setCreator("")
    for number, lines in enumerate(pages):
        body = c.beginText(MARGIN + BODY_MARGIN, top - (BODY_MARGIN if number == 0 else 0))
        body.setFont(font, size, leading)
        for line in lines:
            body.textLine(line)
        c.drawText(body)
        c.showPage()
    c.save()
    return len(pages)


# ---------------------------------------------------------------------------
# Delimited text as an aligned table
#
# Reporting systems export tab-separated text (often under an .xls name) and
# CSV. Printed as plain text, tab stops every 8 characters do not line the
# columns up and wide rows wrap into the next. As a table, each column is as
# wide as 95% of its values, the header row repeats on every page, and free
# text wraps inside its own column.

#: Font sizes and orientations tried in order until the table fits.
TABLE_FORMATS: tuple[tuple[float, tuple[float, float]], ...] = (
    (FONT_SIZE, letter),
    (FONT_SIZE, landscape(letter)),
    (7.5, landscape(letter)),
    (7.0, landscape(letter)),
)
GAP = 2  # spaces between columns
SAMPLE_LINES = 200  # lines looked at to decide whether text is a table
MIN_SHARE = 0.8  # of sampled lines that must have the same number of fields
WIDTH_CAP = 60  # no column is wider than this; longer values wrap
WORD_CAP = 50  # a word longer than this (junk, a pasted URL) may break
MIN_SAVING = 0.9  # a smaller or turned format must need at most 90% of the pages
_NUMBERISH = re.compile(r"^[\d\s.,:/()+\-$%]*$")


def table_delimiter(text: str, allow_comma: bool = False) -> str | None:
    """The delimiter when *text* is a table: most sampled lines split into the
    same number of fields (at least two). Commas only when *allow_comma*
    (a .csv, or an export under a spreadsheet's name), since prose has them."""
    lines = [l for l in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if l.strip()][:SAMPLE_LINES]
    if len(lines) < 2:
        return None
    candidates = [("\t", [l.count("\t") + 1 for l in lines])]
    if allow_comma:
        try:
            candidates.append((",", [len(r) for r in csv.reader(lines)]))
        except csv.Error:
            pass
    for delimiter, counts in candidates:
        fields, n = Counter(counts).most_common(1)[0]
        if fields >= 2 and n >= MIN_SHARE * len(lines):
            return delimiter
    return None


def parse_table(text: str, delimiter: str) -> list[list[str]]:
    """Rows of cleaned cells: whitespace inside a cell collapsed to one
    space (a quoted CSV cell may hold line breaks), controls as in
    :func:`normalize`."""
    text = text.replace("\x00", "")
    if delimiter == "\t":
        raw = [line.split("\t") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    else:
        raw = list(csv.reader(io.StringIO(text, newline="")))
    rows = [[" ".join(normalize(cell).split()) for cell in row] for row in raw]
    while rows and not any(rows[-1]):
        rows.pop()
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


def looks_like_header(row: list[str]) -> bool:
    """A header names columns: mostly filled, no cell that is a number, a
    date or an address. Only a header repeats on every page, so a first row
    of someone's data is never printed (and redacted) a hundred times."""
    filled = [c for c in row if c]
    if len(filled) < max(2, len(row) // 2):
        return False
    return not any(_NUMBERISH.match(c) or "@" in c for c in filled)


@dataclass
class _Layout:
    size: float
    pagesize: tuple[float, float]
    keep: list[int]  # column indexes shown
    widths: list[int]  # characters, per shown column


def _percentile(values: list[int], share: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[int(share * (len(values) - 1))]


def _shrink(widths: list[int], floors: list[int], excess: int) -> int:
    """Take up to *excess* characters from the widest columns, none below its
    floor. Returns what is still left to take."""
    while excess > 0:
        open_ = [i for i in range(len(widths)) if widths[i] > floors[i]]
        if not open_:
            break
        i = max(open_, key=lambda k: widths[k])
        widths[i] -= 1
        excess -= 1
    return excess


def layout_table(rows: list[list[str]], header: bool) -> _Layout | None:
    """Column widths and format for the table; None when it fits no format
    (then it prints as plain text).

    A column starts as wide as its longest value (up to WIDTH_CAP). When a
    row is too wide for a format, columns give way widest first, but never
    below the longest word in them (up to WORD_CAP): cells wrap only at
    spaces, so a name or an address is never split across lines, where
    neither a reader nor whole-word search would find it. Of the formats the
    table fits, a smaller font or a turned page is taken only when it saves
    at least a tenth of the pages: pages are what the reviewers spend their
    time on, but not at any cost in legibility.
    """
    font, _ = _load_font()
    data = rows[1:] if header else rows
    head = rows[0] if header else [""] * len(rows[0])
    keep = [c for c in range(len(rows[0])) if any(r[c] for r in data)]
    if not keep:
        return None
    natural, floors = [], []
    for c in keep:
        values = [r[c] for r in data if r[c]]
        longest_word = max(len(w) for v in values for w in v.split())
        natural.append(max(min(max(len(v) for v in values), WIDTH_CAP), min(len(head[c]), 8), 1))
        floors.append(min(max(longest_word, 4), WORD_CAP, natural[-1]))

    best: tuple[int, _Layout] | None = None
    for size, pagesize in TABLE_FORMATS:
        columns = int((pagesize[0] - 2 * (MARGIN + BODY_MARGIN)) // pdfmetrics.stringWidth("0", font, size))
        widths = list(natural)
        if _shrink(widths, floors, sum(widths) - (columns - GAP * (len(keep) - 1))) > 0:
            continue
        layout = _Layout(size, pagesize, keep, widths)
        pages = _count_pages(rows, header, layout)
        if best is None or pages < MIN_SAVING * best[0]:
            best = (pages, layout)
    return best[1] if best else None


def _lines_per_page(layout: _Layout) -> tuple[int, int]:
    """Lines on the first page (below the body margin) and on the others."""
    usable = layout.pagesize[1] - 2 * MARGIN
    leading = layout.size * 1.35
    return int((usable - BODY_MARGIN) // leading), int(usable // leading)


def _count_pages(rows: list[list[str]], header: bool, layout: _Layout) -> int:
    return len(_paginate(rows, header, layout))


def _cell_lines(value: str, width: int) -> list[str]:
    if len(value) <= width:
        return [value]
    return textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False) or [""]


def _row_lines(row: list[str], layout: _Layout) -> list[str]:
    cells = [_cell_lines(row[c], w) for c, w in zip(layout.keep, layout.widths)]
    height = max(len(c) for c in cells)
    out = []
    for k in range(height):
        parts = [(cell[k] if k < len(cell) else "").ljust(w) for cell, w in zip(cells, layout.widths)]
        out.append((" " * GAP).join(parts).rstrip())
    return out


def _paginate(rows: list[list[str]], header: bool, layout: _Layout) -> list[list[str]]:
    """The table's pages as lines: a note on dropped columns first, the
    header (and a rule) atop every page, rows kept whole unless one is taller
    than a page."""
    font, _ = _load_font()
    first, rest = _lines_per_page(layout)
    top: list[str] = []
    if header:
        top = _row_lines(rows[0], layout)
        top.append((" " * GAP).join("-" * w for w in layout.widths))
    note: list[str] = []
    dropped = [c for c in range(len(rows[0])) if c not in layout.keep]
    if dropped:
        names = [rows[0][c] for c in dropped if header and rows[0][c]]
        what = f"{len(dropped)} column{'' if len(dropped) == 1 else 's'} with no values not shown"
        room = int((layout.pagesize[0] - 2 * (MARGIN + BODY_MARGIN)) // pdfmetrics.stringWidth("0", font, layout.size))
        note = textwrap.wrap((f"{what}: {', '.join(names)}" if names else what) + ".", width=room) + [""]
    pages: list[list[str]] = []
    current = note + top
    fresh = len(current)  # lines on a page before its first row
    for row in rows[1:] if header else rows:
        lines = _row_lines(row, layout)
        limit = first if not pages else rest
        if len(current) + len(lines) > limit and len(current) > fresh:
            pages.append(current)
            current, fresh, limit = list(top), len(top), rest
        while len(current) + len(lines) > limit:  # a row taller than a page
            take = max(1, limit - len(current))
            pages.append(current + lines[:take])
            lines = lines[take:]
            current, fresh, limit = list(top), len(top), rest
        current.extend(lines)
    pages.append(current)
    return pages


def render_table_pdf(text: str, delimiter: str, out_path: Path) -> int:
    """Write delimited *text* as an aligned table (see the section comment);
    return the page count. Falls back to plain text when no format fits the
    table without splitting words. Callers check :func:`covers` first."""
    rows = parse_table(text, delimiter)
    header = bool(rows) and looks_like_header(rows[0])
    layout = layout_table(rows, header) if rows else None
    if layout is None:
        return render_text_pdf(normalize(text), out_path)
    return _write(out_path, _paginate(rows, header, layout), layout.pagesize, layout.size)
