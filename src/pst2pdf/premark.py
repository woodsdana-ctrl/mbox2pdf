"""Pre-mark redactions in volume PDFs.

Adds ``/Redact`` annotations (marked, not applied) wherever a term from an
reviewer-supplied term list or a built-in pattern appears on a page. Acrobat's
Redact tool shows the marks for review; nothing is removed until a person
clicks Apply. The page content streams are never touched: the annotations are
appended as an incremental update.

Coordinates: pdfplumber reports words in a top-left, visually rotated frame.
Annotation rectangles must be in the page's unrotated user space, so every
box is mapped back through the inverse of the rotation transform pdfminer
applied, and Acrobat rotates the annotation along with the page.
"""

from __future__ import annotations

import multiprocessing
import os
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)

from . import embeddedcmap

embeddedcmap.install()  # here, not in apply: the scan workers import this module

DEFAULT_OVERLAY = "REDACTED"
PATTERN_CATEGORY = "pattern"  # ``[pattern]  phone`` lines in a term list activate a pattern
AUTHOR = "pst2pdf"

# ---------------------------------------------------------------------------
# Terms


@dataclass(frozen=True)
class Term:
    """One line of a term list."""

    text: str
    category: str | None = None
    variants: bool = False  # +variants: "Last, First", "F. Last", "Last"
    first: bool = False  # +first: the first name alone (opt-in, it over-matches)

    @property
    def is_pattern(self) -> bool:
        return self.category == PATTERN_CATEGORY

    def phrases(self) -> list[str]:
        """The literal phrases searched for this term, the term itself first."""
        phrases = [self.text]
        if not (self.variants or self.first):
            return phrases
        given, last = _split_name(self.text)
        if not given or not last:
            return phrases
        if self.variants:
            phrases.append(f"{' '.join(given)} {last}")
            phrases.append(f"{last}, {' '.join(given)}")
            initial = given[0].rstrip(".")[:1]
            if initial:
                phrases.append(f"{initial}. {last}")
            phrases.append(last)
        if self.first:
            phrases.append(given[0])
        return _dedupe_phrases(phrases)


def _split_name(text: str) -> tuple[list[str], str]:
    """Return (given names, last name) for "First Last" or "Last, First"."""
    if "," in text:
        last, _, rest = text.partition(",")
        return rest.split(), last.strip()
    words = text.split()
    if len(words) < 2:
        return [], text.strip()
    return words[:-1], words[-1]


def _dedupe_phrases(phrases: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for p in phrases:
        key = " ".join(p.split()).casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(" ".join(p.split()))
    return out


_LINE_RE = re.compile(r"^(?:\[(?P<category>[^\]]*)\])?\s*(?P<rest>.*)$")
_FLAGS = {"+variants": "variants", "+first": "first"}


def load_terms(path: Path | str) -> list[Term]:
    """Parse a term list: ``[category]  text +flags``, ``#`` comments, blanks ignored."""
    terms: list[Term] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        assert m is not None
        category = (m.group("category") or "").strip() or None
        tokens = m.group("rest").split()
        flags: dict[str, bool] = {}
        while tokens and tokens[-1].startswith("+"):
            flag = tokens.pop().lower()
            if flag not in _FLAGS:
                raise ValueError(f"{path}:{lineno}: unknown flag {flag!r}")
            flags[_FLAGS[flag]] = True
        text = " ".join(tokens)
        if not text:
            raise ValueError(f"{path}:{lineno}: no term text on line {raw!r}")
        if category == PATTERN_CATEGORY:
            text = text.lower()
            if text not in PATTERNS:
                raise ValueError(
                    f"{path}:{lineno}: unknown pattern {text!r} (known: {', '.join(PATTERNS)})"
                )
        term = Term(text=text, category=category, **flags)
        _check_category(term, path, lineno)
        terms.append(term)
    return terms


def _check_category(term: Term, path, lineno: int) -> None:
    """A category becomes the overlay text stamped over every hit, so it must
    never contain the term it hides: ``[Doe] Jane Doe`` would print the name
    back on top of the redaction."""
    if not term.category or term.is_pattern:
        return
    needle = term.category.casefold()
    for phrase in term.phrases():
        if needle in phrase.casefold():
            raise ValueError(
                f"{path}:{lineno}: category {term.category!r} appears inside the term {phrase!r}; "
                "the category is printed over the redaction, so it cannot contain the name it hides"
            )


# ---------------------------------------------------------------------------
# Patterns
#
# Each regex is applied to a reconstructed line of text with IGNORECASE. When a
# regex has a group named ``hit`` only that group is marked (the DOB pattern
# marks the date, not the word "born").

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = (
    r"(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    rf"|{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}"
    rf"|\d{{1,2}}\s+{_MONTH},?\s+\d{{4}})"
)
_DOB_LEAD = (
    r"(?:\bd\.?o\.?b\b\.?|\bdate\s+of\s+birth\b|\bbirth\s*(?:date|day)\b|\bborn\b)"
    r"\s*(?:is|of|on|:|-|–|—)?\s*(?:on\s+)?"
)

PATTERNS: dict[str, re.Pattern[str] | None] = {
    # US phone numbers with separators or area-code parentheses; bare 10-digit
    # runs are deliberately not matched because they collide with other IDs.
    "phone": re.compile(
        r"(?<![\d\-.])(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?![\d\-])",
        re.IGNORECASE,
    ),
    "ssn": re.compile(r"(?<![\d\-])\d{3}[\-\s]\d{2}[\-\s]\d{4}(?![\d\-])"),
    "dob": re.compile(_DOB_LEAD + r"(?P<hit>" + _DATE + r")(?!\d)", re.IGNORECASE),
    # Supplied per organization through options.student_id_regex; None = inactive.
    "student-id": None,
}


def resolve_patterns(
    names: Iterable[str] | Mapping[str, str | re.Pattern[str]] | None,
    options=None,
) -> dict[str, re.Pattern[str]]:
    """Turn pattern names (or a name -> regex mapping) into compiled regexes."""
    resolved: dict[str, re.Pattern[str]] = {}
    if names is None:
        names = list(getattr(options, "patterns", None) or [])
    if isinstance(names, Mapping):
        for name, regex in names.items():
            resolved[name] = regex if isinstance(regex, re.Pattern) else re.compile(regex, re.IGNORECASE)
        return resolved
    for name in names:
        key = name.strip().lower()
        if key == "student-id":
            regex = getattr(options, "student_id_regex", None)
            if not regex:
                raise ValueError("pattern 'student-id' needs options.student_id_regex")
            resolved[key] = re.compile(regex, re.IGNORECASE)
        elif key in PATTERNS and PATTERNS[key] is not None:
            resolved[key] = PATTERNS[key]  # type: ignore[assignment]
        else:
            raise ValueError(f"unknown pattern {name!r} (known: {', '.join(PATTERNS)})")
    return resolved


# ---------------------------------------------------------------------------
# Coverage


@dataclass
class Coverage:
    """What apply() found: hits per term, per pattern, and the misses."""

    term_hits: dict[str, int] = field(default_factory=dict)  # term text -> hits (variants included)
    phrase_hits: dict[str, int] = field(default_factory=dict)  # each searched phrase -> hits
    pattern_hits: dict[str, int] = field(default_factory=dict)  # pattern name -> hits
    files: int = 0
    pages: int = 0

    @property
    def terms_without_hits(self) -> list[str]:
        return [t for t, n in self.term_hits.items() if n == 0]

    zero_hit_terms = terms_without_hits  # the name report.py looks for

    @property
    def total(self) -> int:
        return sum(self.term_hits.values()) + sum(self.pattern_hits.values())


# ---------------------------------------------------------------------------
# Matching


@dataclass(frozen=True)
class _Matcher:
    key: str  # term text or pattern name; hits from one key never overlap each other
    kind: str  # "term" | "pattern"
    phrase: str  # the phrase or pattern name, for phrase_hits
    regex: re.Pattern[str]
    category: str | None


@dataclass
class _Hit:
    matcher: _Matcher
    rect: tuple[float, float, float, float]  # user space x0, y0, x1, y1


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    tokens = [re.escape(t) for t in phrase.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(tokens) + r"(?!\w)", re.IGNORECASE)


def _build_matchers(terms: Iterable[Term], patterns: dict[str, re.Pattern[str]]) -> list[_Matcher]:
    matchers: list[_Matcher] = []
    for term in terms:
        if term.is_pattern:
            continue
        # Longest phrase first so "Jane Doe" claims its span before "Doe" can.
        for phrase in sorted(term.phrases(), key=len, reverse=True):
            matchers.append(_Matcher(term.text, "term", phrase, _phrase_regex(phrase), term.category))
    for name, regex in patterns.items():
        matchers.append(_Matcher(name, "pattern", name, regex, name))
    return matchers


# pdfplumber reads rotated pages in device order; these make the words come
# out in reading order for the common case of horizontal text on a page that
# carries a /Rotate.
_DIRECTIONS: dict[int, dict[str, str]] = {
    0: {},
    90: {"line_dir_rotated": "rtl", "char_dir_rotated": "ttb"},
    180: {"line_dir": "btt", "char_dir": "rtl"},
    270: {"line_dir_rotated": "ltr", "char_dir_rotated": "btt"},
}

Box = tuple[float, float, float, float]


def _user_space_mapper(page: pdfplumber.page.Page) -> Callable[[Box], Box]:
    """Map a pdfplumber (x0, top, x1, bottom) box to unrotated PDF user space."""
    rotation = page.rotation
    height = float(page.height)
    # pdfplumber adds its own (already inverted) mediabox origin onto every
    # object it reports (see pdfplumber issue #1181); take it back off first.
    mb_x0, mb_top = float(page.mediabox[0]), float(page.mediabox[1])
    # The raw MediaBox pdfminer used for its rotation transform.
    x0, y0, x1, y1 = (float(v) for v in page.page_obj.mediabox)
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))

    # Inverses of pdfminer's per-rotation CTMs (pdfinterp.process_page):
    #   0:   dx = ux - x0,  dy = uy - y0
    #   90:  dx = uy - y0,  dy = x1 - ux
    #   180: dx = x1 - ux,  dy = y1 - uy
    #   270: dx = y1 - uy,  dy = ux - x0
    def device_to_user(dx: float, dy: float) -> tuple[float, float]:
        if rotation == 90:
            return x1 - dy, dx + y0
        if rotation == 180:
            return x1 - dx, y1 - dy
        if rotation == 270:
            return dy + x0, y1 - dx
        return dx + x0, dy + y0

    def convert(box: Box) -> Box:
        bx0, top, bx1, bottom = box
        dx0, dx1 = bx0 - mb_x0, bx1 - mb_x0
        dy1, dy0 = height - (top - mb_top), height - (bottom - mb_top)
        (ax, ay), (bx, by) = device_to_user(dx0, dy0), device_to_user(dx1, dy1)
        return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)

    return convert


@dataclass
class _Word:
    text: str
    box: Box  # user space
    char_boxes: list[Box]  # one per character of ``text``, user space


def _page_words(page: pdfplumber.page.Page) -> list[_Word]:
    raw = page.extract_words(return_chars=True, **_DIRECTIONS.get(page.rotation, {}))
    if not raw:
        return []
    to_user = _user_space_mapper(page)
    words: list[_Word] = []
    for w in raw:
        text_parts: list[str] = []
        boxes: list[Box] = []
        for ch in w["chars"]:
            t = ch["text"]
            if not t:
                continue
            b = to_user((ch["x0"], ch["top"], ch["x1"], ch["bottom"]))
            text_parts.append(t)
            boxes.extend([b] * len(t))
        text = "".join(text_parts)
        if not text.strip():
            continue
        words.append(_Word(text, to_user((w["x0"], w["top"], w["x1"], w["bottom"])), boxes))
    return words


def _lines(words: list[_Word], tolerance: float = 3.0) -> list[list[_Word]]:
    """Group words into visual lines (by user-space bottom), left to right, and
    split a line where a gap wider than two line heights separates columns.

    Each word is compared with the one before it, not with the line's first:
    a box's bottom is the font's descent, which differs by font on one
    baseline (Noto Color Emoji's sits 2.8 pt above DejaVu's at 10 pt, Noto
    Sans CJK's 0.6 below), so a line of Chinese, a name and an emoji spans
    more than the tolerance and was cut in two between the name's words."""
    ordered = sorted(words, key=lambda w: (-w.box[1], w.box[0]))
    groups: list[list[_Word]] = []
    for w in ordered:
        if groups and abs(groups[-1][-1].box[1] - w.box[1]) <= tolerance:
            groups[-1].append(w)
        else:
            groups.append([w])
    lines: list[list[_Word]] = []
    for group in groups:
        group.sort(key=lambda w: w.box[0])
        height = max(w.box[3] - w.box[1] for w in group)
        segment = [group[0]]
        for prev, cur in zip(group, group[1:]):
            if cur.box[0] - prev.box[2] > 2 * height:
                lines.append(segment)
                segment = []
            segment.append(cur)
        lines.append(segment)
    return lines


def _line_hits(line: list[_Word], matchers: list[_Matcher]) -> list[_Hit]:
    text_parts: list[str] = []
    charmap: list[Box | None] = []
    for i, w in enumerate(line):
        if i:
            text_parts.append(" ")
            charmap.append(None)
        text_parts.append(w.text)
        charmap.extend(w.char_boxes)
    text = "".join(text_parts)
    candidates: list[tuple[int, int, int]] = []  # (start, end, matcher index)
    for index, m in enumerate(matchers):
        group = "hit" if "hit" in m.regex.groupindex else 0
        for match in m.regex.finditer(text):
            start, end = match.span(group)
            if start < end:
                candidates.append((start, end, index))
    # Longest span first, then list order. A candidate inside a span already
    # taken (by any term) is redundant: "Doe" inside "Robert Doe". Two hits of
    # the same term never overlap, so "Doe" is not marked again inside
    # "Jane Doe". Partial overlaps between different terms are both kept.
    candidates.sort(key=lambda c: (-(c[1] - c[0]), c[2], c[0]))
    hits: list[_Hit] = []
    claimed: list[tuple[int, int, str]] = []
    for start, end, index in candidates:
        m = matchers[index]
        if any(
            (s <= start and end <= e) or (k == m.key and s < end and start < e)
            for s, e, k in claimed
        ):
            continue
        boxes = [b for b in charmap[start:end] if b is not None]
        if not boxes:
            continue
        claimed.append((start, end, m.key))
        rect = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        hits.append(_Hit(m, rect))
    hits.sort(key=lambda h: h.rect[0])  # reading order, so /NM numbering follows the line
    return hits


def find_hits(page: pdfplumber.page.Page, matchers: list[_Matcher]) -> list[_Hit]:
    words = _page_words(page)
    if not words:
        return []
    hits: list[_Hit] = []
    for line in _lines(words):
        hits.extend(_line_hits(line, matchers))
    return hits


# ---------------------------------------------------------------------------
# Annotations


def overlay_text(category: str | None, options=None) -> str:
    mapping = getattr(options, "overlay", None) or {}
    return mapping.get(category) or category or DEFAULT_OVERLAY


def _appearance(rect: Box) -> StreamObject:
    """A red 1 pt outline so viewers without their own Redact rendering still
    show the mark. Acrobat replaces it on Apply."""
    x0, y0, x1, y1 = rect
    ap = StreamObject()
    ap[NameObject("/Type")] = NameObject("/XObject")
    ap[NameObject("/Subtype")] = NameObject("/Form")
    ap[NameObject("/BBox")] = ArrayObject(FloatObject(v) for v in rect)
    ap.set_data(f"1 0 0 RG 1 w {x0 + 0.5:.3f} {y0 + 0.5:.3f} {x1 - x0 - 1:.3f} {y1 - y0 - 1:.3f} re S".encode())
    return ap


def redact_annotation(
    hit: _Hit, name: str, options=None, writer: PdfWriter | None = None
) -> DictionaryObject:
    """Build the /Redact dictionary. With ``writer`` the appearance stream is
    registered as an indirect object, which PDF requires for streams."""
    x0, y0, x1, y1 = hit.rect
    category = hit.matcher.category
    annot = DictionaryObject()
    annot[NameObject("/Type")] = NameObject("/Annot")
    annot[NameObject("/Subtype")] = NameObject("/Redact")
    annot[NameObject("/Rect")] = ArrayObject(FloatObject(v) for v in hit.rect)
    # Acrobat's QuadPoints order: upper-left, upper-right, lower-left, lower-right.
    annot[NameObject("/QuadPoints")] = ArrayObject(
        FloatObject(v) for v in (x0, y1, x1, y1, x0, y0, x1, y0)
    )
    annot[NameObject("/OverlayText")] = TextStringObject(overlay_text(category, options))
    annot[NameObject("/DA")] = TextStringObject("/Helv 0 Tf 1 g")  # auto-size, white on the black fill
    annot[NameObject("/Q")] = NumberObject(1)
    annot[NameObject("/IC")] = ArrayObject([FloatObject(0), FloatObject(0), FloatObject(0)])
    annot[NameObject("/C")] = ArrayObject([FloatObject(1), FloatObject(0), FloatObject(0)])
    annot[NameObject("/Contents")] = TextStringObject(category or "")
    annot[NameObject("/Subj")] = TextStringObject("Redact")
    annot[NameObject("/F")] = NumberObject(4)
    annot[NameObject("/T")] = TextStringObject(AUTHOR)
    annot[NameObject("/NM")] = TextStringObject(name)
    appearance = _appearance(hit.rect)
    if writer is not None:
        appearance = writer._add_object(appearance)
    annot[NameObject("/AP")] = DictionaryObject({NameObject("/N"): appearance})
    return annot


# ---------------------------------------------------------------------------
# Entry point


def apply(
    pdf_paths: Iterable[Path | str],
    terms: Iterable[Term],
    patterns: Iterable[str] | Mapping[str, str | re.Pattern[str]] | None = None,
    options=None,
) -> Coverage:
    """Add /Redact annotations to every PDF in place and report coverage.

    ``patterns`` are pattern names (or a name -> regex mapping); ``None`` uses
    ``options.patterns``. ``[pattern]`` lines in the term list activate
    patterns too. Overlay text comes from ``options.overlay`` (category ->
    text) when present, else the category, else "REDACTED".
    """
    terms = list(terms)
    pattern_names: list[str] = []
    if isinstance(patterns, Mapping):
        resolved = resolve_patterns(patterns, options)
    else:
        if patterns is None:
            patterns = list(getattr(options, "patterns", None) or [])
        pattern_names = [p.strip().lower() for p in patterns]
        resolved = resolve_patterns(pattern_names, options)
    for term in terms:
        if term.is_pattern and term.text not in resolved:
            resolved.update(resolve_patterns([term.text], options))
    matchers = _build_matchers(terms, resolved)

    coverage = Coverage()
    for term in terms:
        if not term.is_pattern:
            coverage.term_hits.setdefault(term.text, 0)
            for phrase in term.phrases():
                coverage.phrase_hits.setdefault(phrase, 0)
    for name in resolved:
        coverage.pattern_hits.setdefault(name, 0)

    paths = [Path(p) for p in pdf_paths]
    jobs = max(1, getattr(options, "jobs", None) or os.cpu_count() or 1)
    if jobs == 1 or not matchers:
        for path in paths:
            _premark_file(path, _scan_all(path, matchers), matchers, coverage, options)
        return coverage
    # Finding words is the slow part (pdfminer, pure Python, ~65 ms a page),
    # so it is spread over the pool in page chunks, across every volume at
    # once. Writing the annotations stays in this process, one file at a
    # time and in page order, so the output is identical to a serial run.
    # One core is left to this process, which writes each volume while the
    # workers are still scanning the next (about 15% faster than a full pool).
    workers = max(1, jobs - 1)
    # Not fork: a forked child can inherit a lock some other thread held.
    method = "forkserver" if "forkserver" in multiprocessing.get_all_start_methods() else "spawn"
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context(method)) as pool:
        chunks = {path: [pool.submit(_scan_pages, path, start, stop, matchers) for start, stop in _chunks(path, workers)] for path in paths}
        for path in paths:
            _premark_file(path, [hit for fut in chunks[path] for hit in fut.result()], matchers, coverage, options)
    return coverage


#: Pages per scan job: small enough to balance the pool, large enough that
#: each worker's pdfplumber.open (which reads the whole page tree) is amortized.
_CHUNK_MIN = 25


def _chunks(path: Path, jobs: int) -> list[tuple[int, int]]:
    """[start, stop) page ranges for *path*, about four per worker."""
    total = len(PdfReader(path).pages)
    size = max(_CHUNK_MIN, -(-total // (jobs * 4)))
    return [(start, min(start + size, total)) for start in range(0, total, size)]


def _scan_pages(path: Path, start: int, stop: int, matchers: list[_Matcher]) -> list[tuple[int, list[_Hit]]]:
    """(page index, hits) for pages [start, stop) of *path*. Runs in a worker."""
    found = []
    with pdfplumber.open(path, pages=list(range(start + 1, stop + 1))) as pdf:
        for index, page in zip(range(start, stop), pdf.pages):
            found.append((index, find_hits(page, matchers) if matchers else []))
            page.close()
    return found


def _scan_all(path: Path, matchers: list[_Matcher]) -> list[tuple[int, list[_Hit]]]:
    """(page index, hits) for every page of *path*, in this process."""
    found = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages):
            found.append((index, find_hits(page, matchers) if matchers else []))
            page.close()
    return found


def _premark_file(
    path: Path, pages: list[tuple[int, list[_Hit]]], matchers: list[_Matcher], coverage: Coverage, options
) -> int:
    """Annotate one PDF in place from its scanned *pages* (in page order).
    Returns the number of annotations written."""
    coverage.files += 1
    writer = PdfWriter(path, incremental=True)
    added = 0
    try:
        for index, hits in pages:
            coverage.pages += 1
            if not hits:
                continue
            target = writer.pages[index]
            for n, hit in enumerate(hits, 1):
                name = f"{AUTHOR}-p{index + 1:05d}-{n:04d}"
                writer.add_annotation(target, redact_annotation(hit, name, options, writer))
                added += 1
                m = hit.matcher
                coverage.phrase_hits[m.phrase] = coverage.phrase_hits.get(m.phrase, 0) + 1
                bucket = coverage.term_hits if m.kind == "term" else coverage.pattern_hits
                bucket[m.key] = bucket.get(m.key, 0) + 1
        if added:
            tmp = path.with_name(path.name + ".premark-tmp")
            with open(tmp, "wb") as fh:
                writer.write(fh)
            os.replace(tmp, path)
    finally:
        writer.close()
    return added
