"""Tests for embeddedcmap.py: pdfminer reading fonts whose Encoding is an
embedded CMap, as WeasyPrint writes when a font lacks a glyph.

The fixture PDF is written by hand so its codes, CIDs and widths are known:
codes 0x24 and 0x25 map to themselves ("A", "B"); code 0x100 is a missing
character ("✅") that the CMap sends to CID 0, whose width (500) differs from
the W entry under the code itself (900), as WeasyPrint's can.
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber
import pytest

from pst2pdf import premark  # noqa: F401  (installs the reader, as in a real run)
from pst2pdf.embeddedcmap import EncodingCMap
from pst2pdf.premark import Term, _build_matchers, find_hits

SIZE = 10
X, Y = 20, 50

CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /WP-Encod-0 def
/CMapType 1 def
1 begincodespacerange
<0000> <ffff>
endcodespacerange
3 begincidchar
<0024> 36
<0025> 37
<0100> 0
endcidchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""

TO_UNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <ffff>
endcodespacerange
3 beginbfchar
<0024> <0041>
<0025> <0042>
<0100> <2705>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""


def _stream(body: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s>>\nstream\n%s\nendstream" % (len(body), extra, body)


def write_pdf(path: Path, text: bytes, *, encoding: bytes = b"7 0 R", to_unicode: bool = True) -> Path:
    """One page showing the hex codes *text* in the fixture font."""
    content = b"BT /F1 %d Tf %d %d Td <%s> Tj ET" % (SIZE, X, Y, text)
    tu = b" /ToUnicode 8 0 R" if to_unicode else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        _stream(content),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /Fixture /Encoding %s /DescendantFonts [6 0 R]%s >>" % (encoding, tu),
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Fixture /CIDToGIDMap /Identity"
        b" /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
        b" /W [0 [500] 36 [600 700] 256 [900]] /DW 1000 /FontDescriptor 9 0 R >>",
        _stream(CMAP, b"/Type /CMap /CMapName /WP-Encod-0 "),
        _stream(TO_UNICODE),
        b"<< /Type /FontDescriptor /FontName /Fixture /Flags 32 /FontBBox [0 -200 1000 800]"
        b" /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def _chars(path: Path) -> list[dict]:
    with pdfplumber.open(path) as pdf:
        return list(pdf.pages[0].chars)


def test_text_in_an_embedded_cmap_font_is_read_by_code(tmp_path):
    chars = _chars(write_pdf(tmp_path / "a.pdf", b"0024 0100 0025"))
    assert "".join(c["text"] for c in chars) == "A✅B"


def test_a_missing_glyph_advances_by_its_cids_width_not_its_codes(tmp_path):
    chars = _chars(write_pdf(tmp_path / "a.pdf", b"0024 0100 0025"))
    x = [round(c["x0"], 3) for c in chars]
    # A is 600 wide; the ✅ is drawn as CID 0, 500 wide, not the 900 under its code.
    assert x == [X, X + 600 * SIZE / 1000, X + (600 + 500) * SIZE / 1000]
    assert round(chars[1]["x1"] - chars[1]["x0"], 3) == 500 * SIZE / 1000


def test_premark_finds_a_term_after_a_missing_glyph(tmp_path):
    path = write_pdf(tmp_path / "a.pdf", b"0100 0024 0025")
    with pdfplumber.open(path) as pdf:
        hits = find_hits(pdf.pages[0], _build_matchers([Term("AB")], {}))
    assert len(hits) == 1
    box = hits[0].rect
    assert round(box[0], 3) == X + 500 * SIZE / 1000  # just past the ✅
    assert round(box[2], 3) == X + (500 + 600 + 700) * SIZE / 1000


def test_without_tounicode_the_cmap_still_decodes_to_cids(tmp_path):
    chars = _chars(write_pdf(tmp_path / "a.pdf", b"0024 0025", to_unicode=False))
    assert len(chars) == 2  # were none: pdfminer's empty stand-in CMap decoded nothing
    assert [round(c["x0"], 3) for c in chars] == [X, X + 600 * SIZE / 1000]


def test_named_encodings_are_untouched(tmp_path):
    # /Identity-H with the same codes: 0x100 is then CID 256, 900 wide.
    chars = _chars(write_pdf(tmp_path / "a.pdf", b"0024 0100 0025", encoding=b"/Identity-H"))
    assert "".join(c["text"] for c in chars) == "A✅B"
    assert round(chars[2]["x0"], 3) == X + (600 + 900) * SIZE / 1000


def test_codes_follow_the_codespace_ranges():
    cmap = EncodingCMap.parse(
        b"2 begincodespacerange <00> <80> <8140> <9ffc> endcodespacerange "
        b"1 begincidchar <41> 34 endcidchar "
        b"1 begincidrange <8140> <817e> 633 endcidrange"
    )
    assert list(cmap.codes(b"\x41\x81\x42\x41")) == [b"\x41", b"\x81\x42", b"\x41"]
    assert [cmap.cid(c) for c in cmap.codes(b"\x41\x81\x42")] == [34, 635]
    assert cmap.cid(b"\x7f") == 0  # in the codespace, in no mapping: .notdef


@pytest.mark.parametrize(
    "extra",
    [b"/WMode 1 def", b"/UniJIS-UCS2-H usecmap"],
)
def test_vertical_or_derived_cmaps_are_left_alone(extra):
    cmap = EncodingCMap.parse(extra + b"\n1 begincodespacerange <0000> <ffff> endcodespacerange 1 begincidchar <0024> 36 endcidchar")
    assert not cmap.usable()


def _weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401

        return True
    except Exception:
        return False


@pytest.mark.skipif(not _weasyprint_available(), reason="WeasyPrint system libraries not installed")
def test_premark_reads_a_rendered_message_with_characters_no_font_has(tmp_path):
    """The real trigger: WeasyPrint writes an embedded CMap for DejaVu Sans
    when the text holds characters no installed font has a glyph for. Emoji
    and CJK were that until the image gained fonts for them; Egyptian
    hieroglyphs still are."""
    from pypdf import PdfReader

    from pst2pdf import render

    out = tmp_path / "m.pdf"
    render.render_pdf(
        "<html><body><p>𓀀 𓀁𓀂 Please call Jane Doe about the Fabrikam contract.</p>"
        "<p>於 2026年7月24日 寫道：Jane Doe ✅</p></body></html>",
        out,
    )
    fonts = [f.get_object() for f in PdfReader(out).pages[0]["/Resources"]["/Font"].values()]
    assert any(not isinstance(f["/Encoding"].get_object(), str) for f in fonts), "no embedded CMap: the trigger has moved"
    with pdfplumber.open(out) as pdf:
        page = pdf.pages[0]
        assert "𓀀 𓀁𓀂 Please call" in page.extract_text()
        assert "於 2026年7月24日 寫道：Jane Doe ✅" in page.extract_text()
        hits = find_hits(page, _build_matchers([Term("Jane Doe"), Term("Fabrikam")], {}))
    assert sorted(h.matcher.phrase for h in hits) == ["Fabrikam", "Jane Doe", "Jane Doe"]
