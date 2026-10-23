from pst2pdf.natives import safe_filename


def test_safe_filename():
    assert safe_filename("normal.txt") == "normal.txt"
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename('we<ird">|na?me.doc') == "we_ird___na_me.doc"
    assert safe_filename("") == "attachment"
    assert safe_filename("...") == "attachment"
    long = safe_filename("a" * 300 + ".xlsx")
    assert len(long) <= 120
    assert long.endswith(".xlsx")


def test_keep_natives_writes_each_shown_attachment_once_and_no_withheld_copy(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from pst2pdf import natives
    from pst2pdf.model import Attachment, AttachmentEntry, ConvertedAttachment

    def entry(index, att, status="converted", duplicate_of=None):
        conv = None if duplicate_of else ConvertedAttachment(att, status, "text", Path("x.pdf") if status == "converted" else None)
        return AttachmentEntry(index, 2, att.filename, att.size, att.content_type, att.sha256, conv, duplicate_of=duplicate_of)

    plan = Attachment("plan.txt", b"the plan", "text/plain")
    old = Attachment("Daily.xls", b"old copy", "application/vnd.ms-excel")
    b1 = SimpleNamespace(seq=1, model=SimpleNamespace(attachments=[plan, old]), attachments=[entry(1, plan), entry(2, old, "withheld")])
    b2 = SimpleNamespace(seq=2, model=SimpleNamespace(attachments=[plan]), attachments=[entry(1, plan, duplicate_of=1)])
    root = natives.write_kept([b1, b2], tmp_path)
    assert root.name == "Original attachments - DO NOT RELEASE"
    assert sorted(p.name for p in root.iterdir()) == ["0001-01 plan.txt", "README.txt"]
    readme = (root / "README.txt").read_text()
    assert "must not be released" in readme and "redactable copies" not in readme
