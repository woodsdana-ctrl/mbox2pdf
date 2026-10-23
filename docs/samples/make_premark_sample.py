"""Build docs/samples/premark-sample.pdf for the Acrobat check in
docs/PREMARK-VERIFICATION.md.

Run from the repository root:

    PYTHONPATH=src .venv/bin/python docs/samples/make_premark_sample.py

Page 1 is a fictional memo. Page 2 is the same memo on a page carrying
/Rotate 90, standing in for a landscape scan. Page 3 has no hits. Every
name, number, and date is invented. 555 numbers are reserved for fiction.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from pst2pdf import premark

HERE = Path(__file__).resolve().parent
OUT = HERE / "premark-sample.pdf"
TERMS = HERE / "premark-terms.txt"

MEMO = [
    ("Helvetica-Bold", 14, "MEMORANDUM (fictional sample for redaction pre-marking)"),
    ("Helvetica", 11, "To: Building principal        From: School counselor        Date: September 17, 2026"),
    ("Helvetica", 11, "Re: Attendance follow-up for Jane Doe"),
    ("Helvetica", 11, ""),
    ("Helvetica", 11, "Jane Doe (DOB: 03/14/2015) missed the field trip on Monday. Her father, Robert Doe,"),
    ("Helvetica", 11, "can be reached at (612) 555-0142 or 612-555-0199. The roster lists her as Doe, Jane"),
    ("Helvetica", 11, "and the permission slip was signed J. Doe. Doe returned the slip unsigned, so the"),
    ("Helvetica", 11, "office called JANE DOE's home again on Tuesday."),
    ("Helvetica", 11, ""),
    ("Helvetica", 11, "A fictional SSN, 123-45-6789, is included only so the ssn pattern has something to find."),
    ("Helvetica", 11, "Janet Doerr (staff) is mentioned here to confirm that partial words are NOT marked."),
    ("Helvetica", 11, "The word Jane on its own is not marked either, because +first was not given."),
    ("Helvetica", 11, ""),
    ("Helvetica-Oblique", 10, "Expected marks on this page: 6 student, 1 parent, 2 phone, 1 dob, 1 ssn."),
]


def draw_memo(c: canvas.Canvas, note: str) -> None:
    y = 720
    for font, size, text in MEMO:
        c.setFont(font, size)
        c.drawString(72, y, text)
        y -= size * 1.6
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(72, 72, note)


def build() -> None:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    draw_memo(c, "Page 1: portrait.")
    c.showPage()
    draw_memo(c, "Page 2: the same memo on a page with /Rotate 90, standing in for a landscape scan.")
    c.showPage()
    c.setFont("Helvetica", 11)
    c.drawString(72, 720, "Page 3 contains nothing to mark. It should show no redaction marks.")
    c.showPage()
    c.save()

    reader = PdfReader(io.BytesIO(buf.getvalue()))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        added = writer.add_page(page)
        if index == 1:
            added.rotate(90)
    writer.add_metadata({"/Title": "pst2pdf pre-mark sample", "/Producer": "pst2pdf sample"})
    with open(OUT, "wb") as fh:
        writer.write(fh)

    options = SimpleNamespace(
        patterns=[],
        student_id_regex=None,
        overlay={"student": "FERPA", "parent": "FERPA"},
    )
    coverage = premark.apply([OUT], premark.load_terms(TERMS), None, options)
    print(f"wrote {OUT}")
    print("term hits:   ", coverage.term_hits)
    print("phrase hits: ", coverage.phrase_hits)
    print("pattern hits:", coverage.pattern_hits)
    print("no hits:     ", coverage.terms_without_hits)


if __name__ == "__main__":
    build()
