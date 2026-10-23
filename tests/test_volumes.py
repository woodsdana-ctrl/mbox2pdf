"""End-to-end test of the volume pipeline with a fake renderer (no WeasyPrint)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from pst2pdf import cli, render
from pst2pdf.model import BatesSpec, Options

from conftest import add_headers, make_message, write_eml


def _fake_render_pdf(html: str, out_path: Path) -> render.Rendered:
    """Stand-in for WeasyPrint: message pages are three pages long, every
    other page (placeholders, cross-references, index) is one, and the text
    stays searchable so tests can look for it."""
    pages = 3 if 'class="body"' in html else 1
    kind = "EXCEPTIONS-TABLE" if "Exceptions, page" in html else "INDEX-PAGE" if "Index, page" in html else ""
    c = canvas.Canvas(str(out_path), pagesize=letter)
    for _ in range(pages):
        c.setFont("Helvetica", 9)
        c.drawString(72, 700, html[:80].replace("<", " ").replace(">", " "))
        if kind:
            c.drawString(72, 680, kind)
        c.showPage()
    c.save()
    return render.Rendered()


@pytest.fixture
def fake_render(monkeypatch):
    monkeypatch.setattr(render, "render_pdf", _fake_render_pdf)


@pytest.fixture
def recorded_render(monkeypatch):
    """Like fake_render, but keeps every HTML string rendered, in order."""
    seen: list[str] = []

    def record(html: str, out_path: Path) -> render.Rendered:
        seen.append(html)
        return _fake_render_pdf(html, out_path)

    monkeypatch.setattr(render, "render_pdf", record)
    return seen


def _failing_render(monkeypatch, marker: str, times: int):
    """render_pdf that raises the first *times* calls for a page containing *marker*
    and lacking a fallback notice, then behaves like the fake renderer."""
    calls = {"n": 0}

    def flaky(html: str, out_path: Path) -> render.Rendered:
        if marker in html and 'class="body"' in html and render.HEADER_FALLBACK_NOTICE not in html:
            if times == 2 or render.TEXT_FALLBACK_NOTICE not in html:
                if calls["n"] < times:
                    calls["n"] += 1
                    raise RuntimeError("WeasyPrint choked")
        return _fake_render_pdf(html, out_path)

    monkeypatch.setattr(render, "render_pdf", flaky)
    return calls


def _tree(root: Path) -> None:
    inbox = root / "Inbox"
    sent = root / "Sent Items"
    inbox.mkdir(parents=True)
    sent.mkdir(parents=True)
    a = make_message(subject="Budget", attachments=[("plan.txt", b"the plan")])
    add_headers(a, **{"Message-ID": "<a@example.com>"})
    write_eml(a, inbox / "1.eml")
    b = make_message(subject="Re: Budget", attachments=[("plan.txt", b"the plan")], date="Sat, 16 Mar 2024 10:00:00 -0500")
    add_headers(b, **{"Message-ID": "<b@example.com>", "In-Reply-To": "<a@example.com>"})
    write_eml(b, sent / "2.eml")
    # Same message filed in two folders: shown once, both folders listed.
    write_eml(a, sent / "3.eml")
    write_eml(make_message(subject="Lunch?", date=None), inbox / "4.eml")
    (inbox / "5.vcf").write_text("BEGIN:VCARD\nEND:VCARD\n")


def _options(**kw) -> Options:
    base = dict(request_id="Request 1", jobs=1, convert_attachments=False, bates=BatesSpec(prefix="T-", start=1))
    base.update(kw)
    return Options(**base)


def test_volumes_end_to_end(tmp_path, fake_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(), tmp_path / "work")

    assert stats["errors"] == 0
    assert stats["messages_found"] == 4
    assert stats["messages"] == 3
    assert stats["duplicates_removed"] == 1
    assert stats["skipped_non_mail"] == 1
    assert stats["attachments"] == 2
    assert stats["attachments_crossref"] == 1
    assert stats["attachments_exceptions"] == 0  # conversion off is not a failure
    assert stats["render_fallbacks"] == 0
    assert stats["messages_with_images"] == 0

    names = sorted(p.name for p in out.iterdir())
    assert names == ["00 START HERE.pdf", "Redaction word list.txt", "Volume 01 of 01.pdf", "manifest.json"]

    reader = PdfReader(str(out / "Volume 01 of 01.pdf"))
    labels = reader.page_labels
    assert labels[0] == "T-000001"
    assert labels == [f"T-{i:06d}" for i in range(1, len(reader.pages) + 1)]
    assert reader.metadata.title == "Request 1 — Volume 01 of 01"
    assert reader.metadata.author is None
    def flatten(items):
        for item in items:
            if isinstance(item, list):
                yield from flatten(item)
            else:
                yield item.title

    titles = list(flatten(reader.outline))
    assert titles[0] == "Volume 01 of 01"
    assert titles[1] == "Index"
    assert titles[2].startswith("0001 · 2024-03-15")
    assert not any("Budget" in t or "Alice" in t for t in titles)

    words = (out / "Redaction word list.txt").read_bytes()
    assert b"\r\n" in words
    assert b"Alice Example" in words and b"alice@example.com" in words

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["request_id"] == "Request 1"
    assert manifest["counts"]["messages"] == 3
    assert manifest["counts"]["messages_found"] == 4
    assert manifest["exceptions"] == []  # conversion was off, so placeholders are not exceptions
    assert len(manifest["volumes"]) == 1
    assert manifest["volumes"][0]["sha256"]
    assert stats["manifest"] == str(out / "manifest.json")


def _message_headers(seen: list[str]) -> list[str]:
    return [" ".join(h.split('class="subject"')[0].split()) for h in seen if 'class="header"' in h]


def test_released_headers_say_nothing_about_the_archive(tmp_path, recorded_render):
    # A message number or a count of copies tells a requester how much was
    # collected, and a folder name is whatever the mailbox owner typed.
    _tree(tmp_path / "extract")
    cli.build_volumes(tmp_path / "extract", tmp_path / "out", _options(), tmp_path / "work")
    headers = _message_headers(recorded_render)
    assert len(headers) == 3
    for header in headers:
        assert 'class="meta"' not in header
        assert "Message " not in header and "Folder" not in header and "copies" not in header


def test_triage_headers_keep_number_folders_and_copies(tmp_path, recorded_render):
    # The triage copy is never released, and its attachment index names the
    # carrying messages by number, so the header still gives it.
    _tree(tmp_path / "extract")
    cli.build_volumes(tmp_path / "extract", tmp_path / "out", _options(triage=True), tmp_path / "work")
    headers = _message_headers(recorded_render)
    first = next(h for h in headers if "Message 1<" in h)
    assert "Folders: Inbox, Sent Items" in first and "2 copies in the archive, shown once" in first
    assert any("Message 3<" in h and "Folder: Inbox<" in h for h in headers)


def test_dates_are_converted_to_the_local_zone(tmp_path, fake_render, monkeypatch):
    import time

    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    try:
        extract_root = tmp_path / "extract"
        _tree(extract_root)
        stats = {"calendar_items": 0, "skipped_non_mail": 0, "errors": 0}
        models = cli.parse_tree(extract_root, stats)
        dated = [m for m in models if m.date is not None]
        assert dated and all(m.date.strftime("%Z") in ("CDT", "CST") for m in dated)
        # The clock reading is unchanged, only the zone it is expressed in.
        assert dated[0].date.strftime("%H:%M %Z") == "09:12 CDT"  # -0500 in March is CDT already
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_exceptions_table_is_front_matter_in_volume_one(tmp_path, recorded_render):
    extract_root = tmp_path / "extract"
    inbox = extract_root / "Inbox"
    inbox.mkdir(parents=True)
    write_eml(make_message(subject="Odd file", attachments=[("data.xyz", b"\x00\x01binary")]), inbox / "1.eml")
    write_eml(make_message(subject="Notes", attachments=[("notes.txt", b"plain text")]), inbox / "2.eml")
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(convert_attachments=True), tmp_path / "work")

    assert stats["errors"] == 0
    assert stats["attachments_exceptions"] == 1 and stats["attachments_converted"] == 1
    assert not (out / "Exceptions.pdf").exists()
    reader = PdfReader(str(out / "Volume 01 of 01.pdf"))
    # Index (1 fake page) then the exceptions table (1 fake page), then message 1.
    text_by_page = [p.extract_text() for p in reader.pages]
    assert "INDEX-PAGE" in text_by_page[0] and "EXCEPTIONS-TABLE" in text_by_page[1]
    assert "EXCEPTIONS-TABLE" not in text_by_page[2]
    assert reader.page_labels[2] == "T-000003"
    manifest = json.loads((out / "manifest.json").read_text())
    (exc,) = manifest["exceptions"]
    assert exc["filename"] == "data.xyz" and exc["volume"] == "Volume 01 of 01.pdf"
    assert exc["message"] == 1 and exc["bates"] == "T-000006"  # front 2 + message 3 pages -> placeholder at 6
    html = next(h for h in recorded_render if "Exceptions, page" in h)
    assert "Odd file" not in html  # no Subject column
    assert "data.xyz" in html and "Volume 01 of 01.pdf" in html


def test_render_failure_falls_back_to_plain_text(tmp_path, monkeypatch):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    calls = _failing_render(monkeypatch, "Lunch?", times=1)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(), tmp_path / "work")

    assert calls["n"] == 1
    assert stats["errors"] == 0
    assert stats["messages"] == 3  # the message did not vanish
    assert stats["render_fallbacks"] == 1
    assert stats["render_failures"] == []
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"]["render_fallbacks"] == 1
    assert manifest["exceptions"] == []
    reader = PdfReader(str(out / "Volume 01 of 01.pdf"))
    assert len([t for t in _outline_titles(reader) if "·" in t]) == 3


def test_render_failure_falls_back_to_header_only(tmp_path, monkeypatch):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    calls = _failing_render(monkeypatch, "Lunch?", times=2)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(), tmp_path / "work")

    assert calls["n"] == 2
    assert stats["errors"] == 0
    assert stats["messages"] == 3
    assert stats["render_fallbacks"] == 1
    assert [f["message"] for f in stats["render_failures"]] == [3]
    manifest = json.loads((out / "manifest.json").read_text())
    (exc,) = manifest["exceptions"]
    assert exc["message"] == 3 and exc["attachment"] is None
    assert exc["reason"] == "the message body could not be rendered"
    assert exc["volume"] == "Volume 01 of 01.pdf"
    reader = PdfReader(str(out / "Volume 01 of 01.pdf"))
    assert "EXCEPTIONS-TABLE" in reader.pages[1].extract_text()


def test_page_with_text_outside_it_is_shown_as_text(tmp_path, monkeypatch):
    """A formatted page whose layout left text past the page edge is replaced
    by the plain-text page, which says why, and counted in the manifest."""
    seen = []

    def offpage(html: str, out_path: Path) -> render.Rendered:
        seen.append(html)
        result = _fake_render_pdf(html, out_path)
        if "Lunch?" in html and 'class="body"' in html and render.OFFPAGE_FALLBACK_NOTICE not in html:
            return render.Rendered(offpage=5)
        return result

    monkeypatch.setattr(render, "render_pdf", offpage)
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(), tmp_path / "work")

    assert stats["errors"] == 0
    assert stats["messages"] == 3
    assert stats["offpage_fallbacks"] == 1
    assert stats["render_fallbacks"] == 0
    assert stats["offpage_text"] == 0
    assert sum(render.OFFPAGE_FALLBACK_NOTICE in h for h in seen) == 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"]["offpage_fallbacks"] == 1
    assert manifest["counts"]["offpage_text"] == 0
    assert manifest["exceptions"] == []


def test_report_page_with_text_outside_it_is_counted(tmp_path, monkeypatch):
    def offpage(html: str, out_path: Path) -> render.Rendered:
        result = _fake_render_pdf(html, out_path)
        return render.Rendered(offpage=2) if "Index, page" in html else result

    monkeypatch.setattr(render, "render_pdf", offpage)
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(), tmp_path / "work")

    assert stats["offpage_text"] >= 1
    assert json.loads((out / "manifest.json").read_text())["counts"]["offpage_text"] == stats["offpage_text"]


def test_expanded_zip_is_a_container_not_an_exception(tmp_path, recorded_render):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "alpha")
        zf.writestr("b.txt", "beta")
    extract_root = tmp_path / "extract"
    inbox = extract_root / "Inbox"
    inbox.mkdir(parents=True)
    write_eml(make_message(subject="Records", attachments=[("records.zip", buf.getvalue())]), inbox / "1.eml")
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(), tmp_path / "work")

    assert stats["errors"] == 0
    assert stats["attachments"] == 3
    assert stats["attachments_containers"] == 1
    assert stats["attachments_exceptions"] == 0
    html = next(h for h in recorded_render if "This attachment is a zip archive" in h)
    assert "Its 2 files follow as attachments 2 to 3 of this message." in html
    reader = PdfReader(str(out / "Volume 01 of 01.pdf"))
    # Index, 3 message pages, the container sheet, and two placeholder pages.
    assert len(reader.pages) == 1 + 3 + 1 + 1 + 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"]["attachments_containers"] == 1
    assert manifest["exceptions"] == []


def test_expand_zips_off_leaves_zip_as_placeholder(tmp_path, fake_render):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "alpha")
    extract_root = tmp_path / "extract"
    inbox = extract_root / "Inbox"
    inbox.mkdir(parents=True)
    write_eml(make_message(subject="Records", attachments=[("records.zip", buf.getvalue())]), inbox / "1.eml")
    stats = cli.build_volumes(extract_root, tmp_path / "out", _options(expand_zips=False), tmp_path / "work")
    assert stats["attachments"] == 1 and stats["attachments_containers"] == 0


def test_premark_writes_detail_file_and_keeps_terms_out_of_the_rest(tmp_path, fake_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    terms = tmp_path / "terms.txt"
    terms.write_text("[student]  html\n[parent]  zzznomatch\n")
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(terms_file=terms), tmp_path / "work")
    assert stats["coverage_summary"]["terms_without_hits"] == 1
    detail = (out / "Pre-marked terms - DO NOT RELEASE.txt").read_bytes().decode()
    assert "[student] html:" in detail and "[parent] zzznomatch: 0 marks (no matches)" in detail
    manifest_text = (out / "manifest.json").read_text()
    assert "zzznomatch" not in manifest_text and "terms.txt" not in manifest_text
    manifest = json.loads(manifest_text)
    assert manifest["coverage"]["terms_file_sha256"] == __import__("hashlib").sha256(terms.read_bytes()).hexdigest()
    assert [c["category"] for c in manifest["coverage"]["categories"]] == ["student", "parent"]


def _outline_titles(reader) -> list[str]:
    def flatten(items):
        for item in items:
            if isinstance(item, list):
                yield from flatten(item)
            else:
                yield item.title

    return list(flatten(reader.outline))


def test_volume_split_and_no_dedup(tmp_path, fake_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(
        extract_root, out, _options(volume_pages=2, dedup_messages=False, dedup_attachments=False), tmp_path / "work"
    )
    assert stats["messages"] == 4
    assert stats["attachments_crossref"] == 0
    volumes = sorted(p.name for p in out.glob("Volume *.pdf"))
    assert len(volumes) > 1
    assert volumes[0] == f"Volume 01 of {len(volumes):02d}.pdf"
    # Bates numbering continues across volumes without gaps.
    last = 0
    for name in volumes:
        for label in PdfReader(str(out / name)).page_labels:
            number = int(label.split("-")[1])
            assert number == last + 1
            last = number


def test_premark_marks_terms(tmp_path, fake_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    terms = tmp_path / "terms.txt"
    terms.write_text("[student]  html\n")  # the fake page text contains the html source
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(terms_file=terms), tmp_path / "work")
    assert stats["coverage"].term_hits["html"] > 0
    reader = PdfReader(str(out / "Volume 01 of 01.pdf"))
    kinds = {a.get_object()["/Subtype"] for page in reader.pages for a in page.get("/Annots", [])}
    assert kinds == {"/Redact"}


def test_converted_attachment_gets_no_cover_sheet(tmp_path, recorded_render):
    """Converted pages follow the message directly. Only an attachment with no
    pages of its own gets a sheet standing in for it."""
    extract_root = tmp_path / "extract"
    inbox = extract_root / "Inbox"
    inbox.mkdir(parents=True)
    write_eml(make_message(subject="Budget", attachments=[("plan.txt", b"the plan")]), inbox / "1.eml")
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(convert_attachments=True), tmp_path / "work")

    assert stats["errors"] == 0
    assert stats["attachments_converted"] == 1
    assert not any("Attachment 1 of 1" in html for html in recorded_render)
    # Index, three message pages, and the attachment's one converted page.
    reader = PdfReader(str(out / "Volume 01 of 01.pdf"))
    assert len(reader.pages) == 1 + 3 + 1


def test_no_bates_end_to_end(tmp_path, recorded_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(stamp_bates=False), tmp_path / "work")

    assert stats["errors"] == 0
    assert "bates_first" not in stats
    reader = PdfReader(str(out / "Volume 01 of 01.pdf"))
    assert "/PageLabels" not in reader.trailer["/Root"]
    for page in reader.pages:
        assert "T-0000" not in page.extract_text()
    # Nothing quotes a page number: not the index, the cross-reference or START HERE.
    assert not any("T-0000" in html for html in recorded_render)
    index = next(h for h in recorded_render if "Index, page" in h)
    assert 'class="bates"' not in index
    # The Bates plan is still recorded for the operator, flagged as unprinted.
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["bates"]["stamped"] is False
    assert manifest["bates"]["first"] == "T-000001"
    assert manifest["volumes"][0]["bates_start"] == "T-000001"
    assert manifest["options"]["stamp_bates"] is False


def _labels(path: Path) -> list[int]:
    return [int(label.split("-")[1]) for label in PdfReader(str(path)).page_labels]


def test_appendix_end_to_end(tmp_path, recorded_render, capsys):
    """--appendix-over 0: every converted attachment moves to the appendix.
    plan.txt rides on messages 1 and 2, so with dedup it is one item with two
    pointers, and neither pointer names message 1 as the first copy."""
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(convert_attachments=True, appendix_over=0), tmp_path / "work")

    assert stats["errors"] == 0
    assert stats["attachments"] == 2
    assert stats["attachments_appendix"] == 2 and stats["appendix_items"] == 1
    assert stats["attachments_crossref"] == 0 and stats["attachments_converted"] == 0
    assert stats["appendix_volumes"] == 1
    names = sorted(p.name for p in out.iterdir())
    assert names == [
        "00 START HERE.pdf", "Attachments 01 of 01.pdf", "Redaction word list.txt", "Volume 01 of 01.pdf", "manifest.json",
    ]

    # One Bates sequence: the appendix picks up where the message volume stops.
    volume = _labels(out / "Volume 01 of 01.pdf")
    appendix = _labels(out / "Attachments 01 of 01.pdf")
    assert volume == list(range(1, len(volume) + 1))
    assert appendix == [volume[-1] + 1, volume[-1] + 2]  # header sheet + the one converted page
    assert stats["bates_last"] == f"T-{appendix[-1]:06d}"
    assert stats["pages"] == len(volume) + len(appendix)
    assert (stats["volume_pages"], stats["appendix_pages"]) == (len(volume), len(appendix))
    # The operator's summary counts the two kinds of volume apart.
    cli._print_volume_summary(stats)
    printed = capsys.readouterr().out
    assert f"Volumes: 1, pages T-000001 to T-{volume[-1]:06d}\n" in printed
    assert f"Attachment volumes: 1, pages T-{appendix[0]:06d} to T-{appendix[-1]:06d} (1 item)\n" in printed
    # Index (1), then per message 3 pages + a one-page pointer where plan.txt was.
    assert len(volume) == 1 + (3 + 1) + (3 + 1) + 3

    reader = PdfReader(str(out / "Attachments 01 of 01.pdf"))
    assert reader.metadata.title == "Request 1 — Attachments 01 of 01"
    titles = _outline_titles(reader)
    assert titles[0] == "Attachments 01 of 01"
    assert titles[1].startswith("Item 1 · message 0001 · 2024-03-15")
    assert not any("plan" in t or "Budget" in t for t in titles)

    pointers = [h for h in recorded_render if "This attachment is in a separate volume" in h]
    assert len(pointers) == 2
    for html in pointers:
        assert "Attachments 01 of 01.pdf, at T-0000" in html
        assert "also attached to other messages" in html
        assert "item 1" not in html and "2 messages" not in html
        assert "appears earlier" not in html
    (header,) = [h for h in recorded_render if "Attachment moved out of the message volumes" in h]
    assert "plan.txt" in header and "Attached to these messages" in header and "Item 1" not in header
    start = next(h for h in recorded_render if "Start here" in h)
    assert "1 attachment volume" in start and "Every attachment that was converted to pages is here" in start

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["options"]["appendix_over"] == 0
    (av,) = manifest["appendix_volumes"]
    assert av["filename"] == "Attachments 01 of 01.pdf" and len(av["sha256"]) == 64
    assert av["bates_start"] == f"T-{appendix[0]:06d}"
    (item,) = manifest["appendix_items"]
    assert item["filename"] == "plan.txt" and item["messages"] == [1, 2] and item["pages"] == 1
    assert manifest["counts"]["attachments_appendix"] == 2


def test_appendix_without_attachment_dedup_gives_each_slot_its_own_item(tmp_path, fake_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(
        extract_root, out, _options(convert_attachments=True, appendix_over=0, dedup_attachments=False), tmp_path / "work"
    )
    assert stats["errors"] == 0
    assert stats["attachments_appendix"] == 2 and stats["appendix_items"] == 2
    manifest = json.loads((out / "manifest.json").read_text())
    assert [(i["item"], i["messages"]) for i in manifest["appendix_items"]] == [(1, [1]), (2, [2])]
    assert len(_labels(out / "Attachments 01 of 01.pdf")) == 4


def test_appendix_splits_by_volume_pages(tmp_path, fake_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(
        extract_root, out,
        _options(convert_attachments=True, appendix_over=0, dedup_attachments=False, volume_pages=2),
        tmp_path / "work",
    )
    assert stats["errors"] == 0
    appendix = sorted(p.name for p in out.glob("Attachments *.pdf"))
    assert appendix == ["Attachments 01 of 02.pdf", "Attachments 02 of 02.pdf"]
    volumes = sorted(p.name for p in out.glob("Volume *.pdf"))
    last = 0
    for name in [*volumes, *appendix]:
        for number in _labels(out / name):
            assert number == last + 1
            last = number


def test_appendix_threshold_not_met_changes_nothing(tmp_path, fake_render):
    """With nothing over the threshold the volumes are the ones an off run writes."""
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    off = tmp_path / "off"
    on = tmp_path / "on"
    cli.build_volumes(extract_root, off, _options(convert_attachments=True), tmp_path / "w1")
    stats = cli.build_volumes(extract_root, on, _options(convert_attachments=True, appendix_over=5), tmp_path / "w2")
    assert stats["appendix_items"] == 0 and stats["attachments_appendix"] == 0
    assert not list(on.glob("Attachments *.pdf"))
    a = PdfReader(str(off / "Volume 01 of 01.pdf"))
    b = PdfReader(str(on / "Volume 01 of 01.pdf"))
    assert a.page_labels == b.page_labels
    assert [p.extract_text() for p in a.pages] == [p.extract_text() for p in b.pages]
    manifest = json.loads((on / "manifest.json").read_text())
    assert manifest["appendix_volumes"] == [] and manifest["appendix_items"] == []


def test_premark_covers_appendix_volumes(tmp_path, fake_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    terms = tmp_path / "terms.txt"
    terms.write_text("[student]  html\n")  # the fake page text contains the html source
    out = tmp_path / "out"
    stats = cli.build_volumes(
        extract_root, out, _options(terms_file=terms, convert_attachments=True, appendix_over=0), tmp_path / "work"
    )
    assert stats["errors"] == 0
    reader = PdfReader(str(out / "Attachments 01 of 01.pdf"))
    kinds = {a.get_object()["/Subtype"] for page in reader.pages for a in page.get("/Annots", [])}
    assert kinds == {"/Redact"}


def test_appendix_flag_parse_and_validation(tmp_path, capsys):
    args = cli.build_parser().parse_args(["in.pst", "out", "--appendix-over", "5"])
    assert cli.options_from_args(args).appendix_over == 5
    assert cli.options_from_args(cli.build_parser().parse_args(["in.pst", "out"])).appendix_over is None
    pst = tmp_path / "a.pst"
    pst.write_bytes(b"!BDN")
    with pytest.raises(SystemExit):
        cli.main([str(pst), str(tmp_path / "o"), "--appendix-over", "-1"])
    assert "must not be negative" in capsys.readouterr().err


def test_cli_flags_parse():
    args = cli.build_parser().parse_args(
        ["in.pst", "out", "--order", "chrono", "--bates-prefix", "X-", "--bates-start", "50",
         "--patterns", "phone,ssn", "--overlay", "student=FERPA", "--no-dedup-attachments"]
    )
    opts = cli.options_from_args(args)
    assert opts.order == "chrono"
    assert opts.stamp_bates is True  # on unless --no-bates is given
    assert cli.options_from_args(cli.build_parser().parse_args(["in.pst", "out", "--no-bates"])).stamp_bates is False
    assert opts.bates.label(50) == "X-000050"
    assert opts.patterns == ["phone", "ssn"]
    assert opts.overlay == {"student": "FERPA"}
    assert opts.dedup_attachments is False
    # Never the archive's file name: it lands in the PDF Title and could be a person's name.
    assert opts.request_id.startswith("Request 20")
    assert cli.default_request_id(__import__("datetime").date(2026, 9, 17)) == "Request 2026-09-17"


def test_separate_was_removed(tmp_path, capsys):
    pst = tmp_path / "a.pst"
    pst.write_bytes(b"!BDN")
    with pytest.raises(SystemExit) as exc:
        cli.main([str(pst), str(tmp_path / "o"), "--separate"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--separate was removed" in err and "--triage" in err
