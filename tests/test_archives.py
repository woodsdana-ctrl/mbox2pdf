import io
import zipfile

from pst2pdf import archives
from pst2pdf.model import Attachment


def _zip(entries, **kw) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data, **kw)
    return buf.getvalue()


def test_expand_entries_are_named_under_the_zip():
    att = Attachment("docs.zip", _zip([("a/one.pdf", b"%PDF-1"), ("two.docx", b"PK"), ("dir/", b"")]), "application/zip")
    exp = archives.expand(att, 10_000)
    assert [e.filename for e in exp.entries] == ["docs.zip/a/one.pdf", "docs.zip/two.docx"]
    assert exp.entries[0].content == b"%PDF-1"
    assert exp.skipped == []


def test_not_a_zip_returns_none():
    assert archives.expand(Attachment("notes.txt", b"hello", "text/plain"), 10_000) is None
    assert archives.expand(Attachment("bad.zip", b"PK\x03\x04garbage", "application/zip"), 10_000) is None


def test_path_traversal_is_neutralised():
    att = Attachment("x.zip", _zip([("../../etc/passwd", b"root"), ("/abs.txt", b"a"), ("..\\win.txt", b"w")]), "application/zip")
    exp = archives.expand(att, 10_000)
    assert [e.filename for e in exp.entries] == ["x.zip/etc/passwd", "x.zip/abs.txt", "x.zip/win.txt"]


def _mark_encrypted(data: bytes, name: bytes) -> bytes:
    """Set the encryption flag on *name*'s local and central headers.
    zipfile cannot write encrypted archives, so the bit is patched in."""
    out = bytearray(data)
    for sig, flag_off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        pos = out.find(name)
        while pos != -1:
            header = out.rfind(sig, 0, pos)
            if header != -1:
                out[header + flag_off] |= 0x1
            pos = out.find(name, pos + 1)
    return bytes(out)


def test_oversize_entry_and_encrypted_entry_are_skipped():
    data = _mark_encrypted(_zip([("big.bin", b"x" * 5000), ("small.txt", b"ok"), ("secret.txt", b"???")]), b"secret.txt")
    exp = archives.expand(Attachment("x.zip", data, "application/zip"), 1000)
    assert [e.filename for e in exp.entries] == ["x.zip/small.txt"]
    assert any("big.bin" in s for s in exp.skipped)
    assert any("secret.txt: password protected" in s for s in exp.skipped)


def test_too_many_entries_refused():
    att = Attachment("many.zip", _zip([(f"{i}.txt", b"a") for i in range(archives.MAX_ENTRIES + 1)]), "application/zip")
    exp = archives.expand(att, 10_000)
    assert exp.entries == [] and "none expanded" in exp.skipped[0]


def test_zip_bomb_ratio_refused(monkeypatch):
    monkeypatch.setattr(archives, "RATIO_FLOOR", 1000)
    att = Attachment("bomb.zip", _zip([("zeros.bin", b"\0" * 2_000_000)]), "application/zip")
    exp = archives.expand(att, 10_000_000)
    assert exp.entries == [] and "compression ratio" in exp.skipped[0]


def test_expand_all_keeps_zip_and_does_not_recurse():
    inner = _zip([("deep.txt", b"deep")])
    outer = _zip([("inner.zip", inner), ("top.txt", b"top")])
    atts = [Attachment("a.txt", b"a", "text/plain"), Attachment("outer.zip", outer, "application/zip")]
    out, notes = archives.expand_all(atts, 10_000)
    assert [a.filename for a in out] == ["a.txt", "outer.zip", "outer.zip/inner.zip", "outer.zip/top.txt"]
    assert out[2].content_type == "application/zip"
    assert notes == {}


def test_office_documents_are_not_expanded():
    docx = _zip([("[Content_Types].xml", b"<Types/>"), ("word/document.xml", b"<w/>")])
    assert archives.looks_like_zip(Attachment("memo.docx", docx, "application/octet-stream")) is False
    # Even with a .zip name, an Office package is left for LibreOffice.
    assert archives.expand(Attachment("memo.zip", docx, "application/zip"), 10_000) is None
    odt = _zip([("mimetype", b"application/vnd.oasis.opendocument.text"), ("content.xml", b"<o/>")])
    assert archives.expand(Attachment("memo.zip", odt, "application/zip"), 10_000) is None


def test_unnamed_zip_by_content_type_only():
    data = _zip([("a.txt", b"a")])
    assert archives.looks_like_zip(Attachment("attachment", data, "application/zip")) is True
    assert archives.looks_like_zip(Attachment("attachment", data, "application/octet-stream")) is False
