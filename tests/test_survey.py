"""The page budget (survey.measure), START HERE's largest attachments, the
manifest's largest_attachments, and the --survey run mode.

Everything here runs without WeasyPrint: HTML is asserted as text, and the
end-to-end tests use a stand-in renderer whose plain-text pages hold 59 lines
each, like the real one.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from pst2pdf import assemble, cli, render, report, survey
from pst2pdf.model import (
    AttachmentEntry,
    BatesSpec,
    ConvertedAttachment,
    MessageBundle,
    MessageModel,
    Options,
    Attachment,
)

from conftest import make_message, write_eml

TZ = timezone(timedelta(hours=-5))
BASE = datetime(2026, 3, 2, 9, 15, tzinfo=TZ)


# --- hand-built bundles -----------------------------------------------------


def _model(seq: int) -> MessageModel:
    return MessageModel(
        source=Path(f"/work/Inbox/{seq}.eml"),
        folder="Inbox",
        subject=f"Message {seq}",
        from_addr="Alice Example <alice@example.com>",
        to_addrs="Bob Example <bob@example.com>",
        cc_addrs="",
        date=BASE + timedelta(days=seq),
        body_html="<p>x</p>",
    )


def _conv(name: str, pages: int, status: str = "converted", method: str = "text", reason: str = "") -> ConvertedAttachment:
    att = Attachment(name, name.encode(), "application/octet-stream")
    has_pdf = status in ("converted", "passthrough")
    return ConvertedAttachment(att, status, method, Path(f"/work/{name}.pdf") if has_pdf else None, pages if has_pdf else 0, reason)


class World:
    """Bundles for measure(): attachments keyed by name, each with its own sha."""

    def __init__(self, dedup: bool = True):
        self.dedup = dedup
        self.bundles: list[MessageBundle] = []
        self.first: dict[str, int] = {}

    def message(self, *attachments: tuple[str, ConvertedAttachment], pages: int = 2) -> MessageBundle:
        seq = len(self.bundles) + 1
        bundle = MessageBundle(seq=seq, model=_model(seq), folders=["Inbox"], thread_id="t", message_pdf=Path(f"/m/{seq}.pdf"), message_pages=pages)
        for index, (name, conv) in enumerate(attachments, start=1):
            sha = "sha-" + name
            dup = None
            if self.dedup and name in self.first and self.first[name] != seq:
                dup = self.first[name]
            else:
                self.first.setdefault(name, seq)
            bundle.attachments.append(
                AttachmentEntry(
                    index=index,
                    total=len(attachments),
                    filename=name,
                    size=1000 * index,
                    content_type="application/octet-stream",
                    sha256=sha,
                    converted=None if dup else conv,
                    duplicate_of=dup,
                )
            )
        self.bundles.append(bundle)
        return bundle


def _world(dedup: bool = True) -> World:
    log = _conv("server.log", 850)
    sheet = _conv("budget.xlsx", 40, method="libreoffice")
    photo = _conv("photo.jpg", 1, method="image")
    song = _conv("song.mp3", 0, status="placeholder", method="none", reason="unsupported file type (.mp3)")
    w = World(dedup)
    w.message(("server.log", log), ("photo.jpg", photo))
    w.message(("server.log", log))
    w.message(("budget.xlsx", sheet), ("song.mp3", song))
    w.message(("server.log", log), ("budget.xlsx", sheet))
    return w


def _opts(**kw) -> Options:
    base = dict(request_id="Request 1", jobs=1, bates=BatesSpec(prefix="T-", start=1))
    base.update(kw)
    return Options(**base)


def test_measure_dedup_counts_pages_once_and_later_copies_as_notes():
    w = _world(dedup=True)
    budget = survey.measure(w.bundles, _opts(), front_matter_pages=2)
    by_name = {a.filename: a for a in budget.attachments}

    log = by_name["server.log"]
    assert (log.pages, log.copies, log.full_copies, log.pages_in_volumes) == (850, 3, 1, 850)
    assert (log.first_seq, log.first_date) == (1, BASE + timedelta(days=1))
    sheet = by_name["budget.xlsx"]
    assert (sheet.copies, sheet.pages_in_volumes) == (2, 40)
    # Placeholders put no pages of their own in the volumes, but are listed.
    song = by_name["song.mp3"]
    assert (song.pages, song.pages_in_volumes, song.status) == (0, 0, "placeholder")
    assert song.reason == "unsupported file type (.mp3)"

    assert [a.filename for a in budget.attachments] == ["server.log", "budget.xlsx", "photo.jpg", "song.mp3"]
    assert budget.messages == 4 and budget.message_pages == 8
    assert budget.attachment_slots == 7 and budget.distinct_attachments == 4
    assert budget.attachment_pages == 850 + 40 + 1
    # Cross-references: server.log twice and budget.xlsx once; the mp3's placeholder.
    assert budget.note_pages == 4
    assert budget.exceptions == 1
    assert budget.total_pages == 2 + 8 + 891 + 4


def test_measure_without_dedup_counts_every_copy_in_full():
    budget = survey.measure(_world(dedup=False).bundles, _opts(dedup_attachments=False))
    by_name = {a.filename: a for a in budget.attachments}
    assert (by_name["server.log"].full_copies, by_name["server.log"].pages_in_volumes) == (3, 2550)
    assert by_name["budget.xlsx"].pages_in_volumes == 80
    assert budget.attachment_pages == 2550 + 80 + 1
    assert budget.note_pages == 1  # only the placeholder
    assert budget.total_pages == 8 + 2631 + 1


def test_measure_by_type_groups_by_method():
    budget = survey.measure(_world().bundles, _opts())
    rows = [(t.label, t.attachments, t.copies, t.pages) for t in budget.by_type]
    assert rows == [
        ("Plain text", 1, 3, 850),
        ("Office file", 1, 2, 40),
        ("Picture", 1, 1, 1),
        ("Not converted", 1, 1, 0),
    ]


def test_measure_with_a_plan_matches_the_plan_exactly():
    w = _world(dedup=True)
    opts = _opts(volume_pages=500)
    plan = assemble.plan(w.bundles, opts, front_matter_pages=3)
    budget = survey.measure(w.bundles, opts, plan)
    assert budget.front_matter_pages == 3
    assert budget.total_pages == sum(v.page_count for v in plan.volumes)
    assert budget.volumes == len(plan.volumes) == 2  # the 850-page log fills volume 1
    assert budget.total_pages == 3 + budget.message_pages + budget.attachment_pages + budget.note_pages


def test_what_if_math():
    budget = survey.measure(_world(dedup=True).bundles, _opts(volume_pages=100), front_matter_pages=1)
    total = budget.total_pages
    rows = {w.threshold: w for w in budget.what_if}
    assert list(rows) == [10, 25, 50, 100, 250, 500]
    # Longer than 25: the log (850) and the sheet (40); each leaves one note.
    assert (rows[25].attachments, rows[25].pages_moved, rows[25].notes_added) == (2, 890, 2)
    assert rows[25].pages_left == total - 890 + 2
    assert rows[25].volumes_left == math.ceil(rows[25].pages_left / 100)
    # Longer than 50 and up to 500: the log alone.
    for t in (50, 100, 250, 500):
        assert (rows[t].attachments, rows[t].pages_moved, rows[t].pages_left) == (1, 850, total - 849)

    nodedup = survey.measure(_world(dedup=False).bundles, _opts(dedup_attachments=False, volume_pages=100))
    row = next(w for w in nodedup.what_if if w.threshold == 500)
    # Every full copy moves, and each leaves a note.
    assert (row.pages_moved, row.notes_added) == (2550, 3)
    assert row.pages_left == nodedup.total_pages - 2550 + 3


def test_what_if_ignores_attachments_at_the_threshold():
    rows = survey.what_if(
        [survey.AttachmentSize("s", "a.txt", "", 1, "converted", "text", "", 10, 1, 1, 1, None)], 100, 1000, (10,)
    )
    assert rows[0].attachments == 0 and rows[0].pages_left == 100


def test_share_text():
    assert survey.share_text(61, 100) == "61%"
    assert survey.share_text(42, 1000) == "4.2%"
    assert survey.share_text(1, 10_000) == "<0.1%"
    assert survey.share_text(0, 100) == "0%"
    assert survey.share_text(5, 0) == "0%"


# --- CSV --------------------------------------------------------------------


def test_csv_lists_every_distinct_attachment_largest_first(tmp_path):
    budget = survey.measure(_world(dedup=True).bundles, _opts())
    path = survey.write_csv(budget, tmp_path)
    raw = path.read_bytes()
    assert path.name == "Attachment survey.csv"
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM, for Excel
    assert b"\r\n" in raw
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert list(rows[0]) == list(survey.CSV_COLUMNS)
    assert [r["filename"] for r in rows] == ["server.log", "budget.xlsx", "photo.jpg", "song.mp3"]
    log = rows[0]
    assert log["rank"] == "1" and log["type"] == "Plain text" and log["method"] == "text"
    assert (log["pages"], log["copies"], log["pages_in_volumes"]) == ("850", "3", "850")
    assert float(log["share_percent"]) == pytest.approx(100 * 850 / budget.total_pages, abs=0.01)
    assert log["first_message"] == "1" and log["first_message_date"].startswith("2026-03-03T09:15")
    assert log["sha256"] == "sha-server.log"
    song = rows[-1]
    assert (song["status"], song["pages"], song["pages_in_volumes"]) == ("placeholder", "0", "0")
    assert song["reason"] == "unsupported file type (.mp3)"


def test_csv_neutralizes_formula_filenames():
    w = World()
    w.message(("=HYPERLINK(\"http://x\").txt", _conv("evil", 3)), ("-2+3.txt", _conv("minus", 2)))
    text = survey.build_csv(survey.measure(w.bundles, _opts()))
    rows = list(csv.reader(io.StringIO(text)))
    names = [r[1] for r in rows[1:]]
    assert names == ["'=HYPERLINK(\"http://x\").txt", "'-2+3.txt"]


# --- START HERE and the manifest ------------------------------------------


def _text(html: str) -> str:
    return " ".join(unescape(__import__("re").sub(r"<[^>]+>", " ", html)).split())


def test_start_here_lists_largest_attachments():
    w = _world()
    opts = _opts()
    plan = assemble.plan(w.bundles, opts, front_matter_pages=1)
    budget = survey.measure(w.bundles, opts, plan)
    html = report.build_start_here_html(opts, {"messages": 4, "page_budget": budget}, plan)
    text = _text(html)
    assert "Largest attachments" in text
    total = budget.total_pages
    assert f"The 3 attachments with pages fill 891 of the {total:,} pages" in text
    assert "server.log Plain text 850 3 850" in text
    assert "song.mp3" not in text  # no pages of its own
    assert "each later copy is a one-page note" in text


def test_start_here_top_ten_sentence_and_limit():
    w = World()
    for i in range(12):
        w.message((f"file{i:02d}.txt", _conv(f"file{i:02d}", 100 - i)))
    budget = survey.measure(w.bundles, _opts())
    ctx = survey.start_here_context(budget)
    assert ctx["count"] == 10 and ctx["with_pages"] == 12
    text = _text(report.build_start_here_html(_opts(), {"page_budget": budget}, None))
    assert "The 10 largest attachments fill 955 of the" in text
    assert "all 12 attachments with pages fill 1,134" in text
    assert "file09.txt" in text and "file10.txt" not in text


def test_start_here_omits_the_section_without_converted_attachments():
    w = World()
    w.message(("song.mp3", _conv("song", 0, status="placeholder", method="none", reason="x")))
    budget = survey.measure(w.bundles, _opts())
    assert survey.start_here_context(budget) is None
    assert "Largest attachments" not in report.build_start_here_html(_opts(), {"page_budget": budget}, None)
    assert "Largest attachments" not in report.build_start_here_html(_opts(), {}, None)


def test_manifest_lists_the_top_25_with_sha256(tmp_path):
    w = World()
    for i in range(30):
        w.message((f"f{i:02d}.txt", _conv(f"f{i:02d}", 200 - i)))
    budget = survey.measure(w.bundles, _opts())
    report.write_manifest(tmp_path, _opts(), {"page_budget": budget}, None, [])
    doc = json.loads((tmp_path / "manifest.json").read_text())
    rows = doc["largest_attachments"]
    assert len(rows) == 25
    assert rows[0] == {
        "rank": 1,
        "filename": "f00.txt",
        "type": "Plain text",
        "method": "text",
        "pages": 200,
        "copies": 1,
        "pages_in_volumes": 200,
        "share_percent": round(100 * 200 / budget.total_pages, 2),
        "first_message": 1,
        "sha256": "sha-f00.txt",
    }
    assert "page_budget" not in doc["counts"]
    assert "survey" not in doc  # only a survey run carries the projection block


# --- the survey PDF's HTML ------------------------------------------------


def test_survey_html_in_plain_words():
    w = _world()
    opts = _opts(volume_pages=500)
    plan = assemble.plan(w.bundles, opts, front_matter_pages=2)
    budget = survey.measure(w.bundles, opts, plan)
    html = survey.build_survey_html(opts, budget, {"messages_found": 4, "started": BASE})
    assert "<title>" not in html
    text = _text(html)
    assert "Attachment survey" in text and "Request 1, prepared March 2, 2026" in text
    assert "Keep this back." in text
    assert f"about {budget.total_pages:,} pages in 2 volumes of up to 500 pages each" in text
    assert "The 3 attachments with pages would fill 891" not in text  # three of three: worded as "all"
    assert "all 3 attachments with pages would fill 891" in text
    assert "Plain text 1 3 850" in text  # by type: files, attached, pages
    assert "1 server.log Plain text 850 3 850" in text
    assert "1 attachment could not be converted" in text
    assert "Longer than" in text and "500 pages 1 850" in text
    assert "Attachment survey.csv lists every attachment" in text
    assert "identical attachments shown in full once" in text


def test_survey_html_when_conversion_is_off():
    w = World()
    w.message(("a.txt", _conv("a", 0, status="placeholder", method="none", reason="attachment conversion was turned off for this run")))
    opts = _opts(convert_attachments=False)
    budget = survey.measure(w.bundles, opts)
    assert budget.exceptions == 0
    text = _text(survey.build_survey_html(opts, budget))
    assert "Attachments were not converted for this survey" in text
    assert "nothing to set aside" in text
    assert "could not be converted" not in text


# --- end to end with a stand-in renderer ------------------------------------

LINES_PER_PAGE = 59  # what WeasyPrint fits of a plain-text attachment on a Letter page


def _paged_render(html: str, out_path: Path) -> render.Rendered:
    """Message pages are three pages; a plain-text attachment gets one page per
    59 lines, like the real renderer; everything else is one page."""
    if 'class="body"' in html:
        pages = 3
    elif "<pre>" in html:
        body = html.split("<pre>", 1)[1].split("</pre>", 1)[0]
        pages = max(1, math.ceil(body.count("\n") / LINES_PER_PAGE))
    else:
        pages = 1
    c = canvas.Canvas(str(out_path), pagesize=letter)
    for _ in range(pages):
        c.drawString(72, 700, "page")
        c.showPage()
    c.save()
    return render.Rendered()


@pytest.fixture
def paged_render(monkeypatch):
    seen: list[str] = []

    def record(html: str, out_path: Path) -> render.Rendered:
        seen.append(html)
        return _paged_render(html, out_path)

    monkeypatch.setattr(render, "render_pdf", record)
    return seen


BIG_LOG_LINES = 50_000


def _log_tree(root: Path, copies: int = 4) -> None:
    """A 50,000-line log attached to several messages, plus ordinary mail."""
    inbox = root / "Inbox"
    inbox.mkdir(parents=True)
    log = "".join(f"2026-09-01 12:{i // 60 % 60:02d}:{i % 60:02d} INFO worker 7 processed record {i}\n" for i in range(BIG_LOG_LINES))
    big = log.encode()
    for n in range(copies):
        write_eml(
            make_message(subject=f"Nightly job {n}", attachments=[("sync.log", big)], date=f"Mon, {n + 1:02d} Sep 2025 06:00:00 -0500"),
            inbox / f"{n + 1}.eml",
        )
    write_eml(make_message(subject="Notes", attachments=[("notes.txt", b"one\ntwo\n")]), inbox / "10.eml")
    write_eml(make_message(subject="Odd", attachments=[("data.xyz", b"\x00\x01")]), inbox / "11.eml")
    write_eml(make_message(subject="Lunch?"), inbox / "12.eml")


def _volume_pages(out: Path) -> int:
    return sum(len(PdfReader(str(p)).pages) for p in out.glob("Volume *.pdf"))


@pytest.mark.parametrize("dedup", [True, False])
def test_survey_projection_matches_a_full_run(tmp_path, paged_render, dedup):
    extract_root = tmp_path / "extract"
    _log_tree(extract_root)
    opts = _opts(dedup_attachments=dedup, volume_pages=1000)

    survey_out = tmp_path / "survey"
    stats = cli.build_volumes(extract_root, survey_out, Options(**{**opts.__dict__, "survey": True}), tmp_path / "work-survey")
    assert stats["errors"] == 0
    assert sorted(p.name for p in survey_out.iterdir()) == ["Attachment survey.csv", "Attachment survey.pdf", "manifest.json"]

    budget = stats["page_budget"]
    log_pages = math.ceil(BIG_LOG_LINES / LINES_PER_PAGE)
    top = budget.attachments[0]
    assert (top.filename, top.pages, top.copies) == ("sync.log", log_pages, 4)
    assert top.pages_in_volumes == (log_pages if dedup else 4 * log_pages)
    assert budget.exceptions == 1  # data.xyz
    rows = {w.threshold: w for w in budget.what_if}
    assert rows[500].attachments == 1 and rows[500].pages_moved == top.pages_in_volumes

    rows_csv = list(csv.DictReader(io.StringIO((survey_out / "Attachment survey.csv").read_text(encoding="utf-8-sig"))))
    assert rows_csv[0]["filename"] == "sync.log" and rows_csv[0]["pages"] == str(log_pages)
    assert [r["filename"] for r in rows_csv] == ["sync.log", "notes.txt", "data.xyz"]

    manifest = json.loads((survey_out / "manifest.json").read_text())
    assert manifest["options"]["survey"] is True
    assert manifest["survey"]["projected_pages"] == budget.total_pages
    assert manifest["volumes"] == []
    assert manifest["largest_attachments"][0]["sha256"] == top.sha256
    assert [e["filename"] for e in manifest["exceptions"]] == ["data.xyz"]

    survey_html = next(h for h in paged_render if "Attachment survey" in h and "Keep this back" in h)
    assert "sync.log" in survey_html

    # Now the real thing, with the same options: the projection must hold.
    full_out = tmp_path / "full"
    full = cli.build_volumes(extract_root, full_out, opts, tmp_path / "work-full")
    assert full["errors"] == 0
    actual = _volume_pages(full_out)
    assert budget.total_pages == actual == full["pages"]
    assert budget.volumes == full["volumes"]
    assert full["page_budget"].total_pages == actual

    start_here = next(h for h in paged_render if "Start here" in h and "Largest attachments" in h)
    assert "sync.log" in start_here
    assert json.loads((full_out / "manifest.json").read_text())["largest_attachments"][0]["filename"] == "sync.log"


def test_survey_respects_conversion_off(tmp_path, paged_render):
    extract_root = tmp_path / "extract"
    _log_tree(extract_root, copies=2)
    opts = _opts(convert_attachments=False, survey=True)
    stats = cli.build_volumes(extract_root, tmp_path / "out", opts, tmp_path / "work")
    budget = stats["page_budget"]
    assert budget.attachment_pages == 0 and budget.exceptions == 0
    assert all(a.pages == 0 for a in budget.attachments)
    full = cli.build_volumes(extract_root, tmp_path / "full", _opts(convert_attachments=False), tmp_path / "work-full")
    assert budget.total_pages == full["pages"]


# --- command line and wrapper -----------------------------------------------


def test_cli_survey_flag():
    args = cli.build_parser().parse_args(["in.pst", "out", "--survey"])
    assert cli.options_from_args(args).survey is True
    assert cli.options_from_args(cli.build_parser().parse_args(["in.pst", "out"])).survey is False


def test_cli_survey_ignores_premark_options(tmp_path, monkeypatch, capsys):
    pst = tmp_path / "a.pst"
    pst.write_bytes(b"!BDN")
    terms = tmp_path / "terms.txt"
    terms.write_text("[student] Jane Doe\n")
    seen = {}

    def fake_extract(pst_path, work_dir):
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def fake_build(extract_root, output_dir, options, work):
        seen["options"] = options
        return {"errors": 0, "messages_found": 0, "duplicates_removed": 0, "survey_pdf": "s.pdf", "survey_csv": "s.csv",
                "page_budget": survey.measure([], options)}

    monkeypatch.setattr(cli.extract, "extract_pst", fake_extract)
    monkeypatch.setattr(cli, "build_volumes", fake_build)
    code = cli.main([str(pst), str(tmp_path / "out"), "--survey", "--terms", str(terms), "--patterns", "phone", "--keep-natives"])
    assert code == 0
    err = capsys.readouterr()
    assert "--terms, --patterns, --keep-natives are ignored" in err.err
    assert "Projected: 0 pages in 0 volumes" in err.out
    opts = seen["options"]
    assert opts.survey and opts.terms_file is None and opts.patterns == [] and not opts.keep_natives


# The wrapper validates flags itself, so --survey is checked there too.
# (Kept here rather than in test_wrapper.py; the helpers are shared.)
from test_wrapper import WRAPPER, _run_wrapper  # noqa: E402

needs_bash = pytest.mark.skipif(
    __import__("shutil").which("bash") is None or not WRAPPER.is_file(), reason="bash and the wrapper script are needed"
)


@needs_bash
def test_wrapper_passes_survey_and_points_at_the_survey(tmp_path):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc, argv = _run_wrapper(tmp_path, "--survey", "--volume-pages", "500", str(archive))
    assert proc.returncode == 0, proc.stderr
    assert "--survey" in argv and ("--volume-pages", "500") in list(zip(argv, argv[1:]))
    assert "Surveying" in proc.stdout
    assert "Survey done" in proc.stdout
    assert str((tmp_path / "a-pdfs").resolve() / "Attachment survey.pdf") in proc.stdout


@needs_bash
def test_wrapper_help_mentions_survey(tmp_path):
    proc, _ = _run_wrapper(tmp_path, "--help")
    assert proc.returncode == 0 and "--survey" in proc.stdout and "Attachment survey.pdf" in proc.stdout


# --- real rendering (Docker image only) -------------------------------------


def _has_weasyprint() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:  # noqa: BLE001 - missing Pango/Cairo raises OSError, not ImportError
        return False
    return True


@pytest.mark.skipif(not _has_weasyprint(), reason="WeasyPrint (Pango/Cairo) is needed for real page counts")
def test_survey_with_real_rendering_counts_a_long_log(tmp_path):
    """A very long log in miniature, laid out by WeasyPrint for real:
    a 50,000-line log attached to four messages. The survey must find it,
    count it at about 59 lines a page, and project what a full run makes."""
    extract_root = tmp_path / "extract"
    _log_tree(extract_root)
    opts = _opts(jobs=2)
    stats = cli.build_volumes(extract_root, tmp_path / "survey", Options(**{**opts.__dict__, "survey": True}), tmp_path / "w1")
    assert stats["errors"] == 0
    budget = stats["page_budget"]
    top = budget.attachments[0]
    assert top.filename == "sync.log" and top.copies == 4 and top.full_copies == 1
    assert abs(top.pages - BIG_LOG_LINES / LINES_PER_PAGE) < 0.05 * BIG_LOG_LINES / LINES_PER_PAGE
    reader = PdfReader(str(tmp_path / "survey" / "Attachment survey.pdf"))
    text = "".join(p.extract_text() for p in reader.pages)
    assert "sync.log" in text and "Longer than" in text

    full = cli.build_volumes(extract_root, tmp_path / "full", opts, tmp_path / "w2")
    assert full["errors"] == 0
    assert budget.total_pages == _volume_pages(tmp_path / "full")


# --- with --appendix-over ------------------------------------------------------


def _all_volume_pages(out: Path) -> int:
    return sum(len(PdfReader(str(p)).pages) for p in [*out.glob("Volume *.pdf"), *out.glob("Attachments *.pdf")])


@pytest.mark.parametrize("dedup", [True, False])
def test_survey_projection_matches_a_full_run_with_the_appendix(tmp_path, paged_render, dedup):
    """--appendix-over moves the log out: its pages count once per appendix
    item (plus a header sheet), every carrier is a one-page pointer, and the
    total takes in the appendix volumes, matching a full run exactly."""
    extract_root = tmp_path / "extract"
    _log_tree(extract_root)
    opts = _opts(dedup_attachments=dedup, volume_pages=1000, appendix_over=100)

    survey_out = tmp_path / "survey"
    stats = cli.build_volumes(extract_root, survey_out, Options(**{**opts.__dict__, "survey": True}), tmp_path / "work-survey")
    assert stats["errors"] == 0
    budget = stats["page_budget"]
    log_pages = math.ceil(BIG_LOG_LINES / LINES_PER_PAGE)
    items = 1 if dedup else 4
    assert budget.appendix_over == 100 and budget.appendix_items == items
    assert budget.appendix_pages == items * (1 + log_pages)
    top = budget.attachments[0]
    assert (top.filename, top.copies, top.pages_in_volumes) == ("sync.log", 4, items * log_pages)
    assert budget.attachment_pages == items * log_pages + 1  # the log in the appendix, plus notes.txt inline
    assert budget.what_if == []

    full_out = tmp_path / "full"
    full = cli.build_volumes(extract_root, full_out, opts, tmp_path / "work-full")
    assert full["errors"] == 0
    assert full["appendix_items"] == items
    assert budget.total_pages == full["pages"] == _all_volume_pages(full_out)
    assert budget.volumes == len(list(full_out.glob("Volume *.pdf")))
    assert budget.appendix_volumes == len(list(full_out.glob("Attachments *.pdf")))
    parts = budget.front_matter_pages + budget.message_pages + budget.attachment_pages + budget.note_pages
    assert parts == budget.total_pages


def test_survey_html_with_the_appendix_skips_the_what_if_table(tmp_path, paged_render):
    extract_root = tmp_path / "extract"
    _log_tree(extract_root, copies=2)
    opts = _opts(volume_pages=1000, appendix_over=100, survey=True)
    cli.build_volumes(extract_root, tmp_path / "survey", opts, tmp_path / "work")
    html = _text(next(h for h in paged_render if "Attachment survey" in h and "Keep this back" in h))
    assert "attachment volume" in html
    assert "already sets attachments longer than 100 pages aside" in " ".join(html.split())
    assert "Pages left in the message volumes" not in html
