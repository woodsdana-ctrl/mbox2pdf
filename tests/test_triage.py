"""--triage: attachments as original files, an index for the reviewers, triage-copy volumes."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader

from pst2pdf import cli, decisions, natives
from pst2pdf.model import Attachment, BatesSpec, Options

from conftest import make_message, write_eml
from test_volumes import recorded_render  # noqa: F401  (fixture)

DOCX = b"PK\x03\x04" + b"\x00" * 26  # a zip header is all the type check reads


def _zip(**files: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _tree(root: Path) -> None:
    inbox = root / "Inbox"
    inbox.mkdir(parents=True)
    write_eml(
        make_message(subject="Budget", attachments=[("plan.txt", b"the plan"), ("Budget.xlsx", b"first budget")],
                     date="Mon, 02 Mar 2026 09:00:00 -0600"),
        inbox / "1.eml",
    )
    write_eml(
        make_message(subject="Re: Budget", attachments=[("plan.txt", b"the plan"), ("Budget.xlsx", b"second budget")],
                     date="Tue, 03 Mar 2026 09:00:00 -0600"),
        inbox / "2.eml",
    )
    write_eml(
        make_message(subject="Records", attachments=[("records.zip", _zip(**{"a.txt": b"alpha", "b.txt": b"beta"}))],
                     date="Wed, 04 Mar 2026 09:00:00 -0600"),
        inbox / "3.eml",
    )
    write_eml(
        make_message(subject="Odd names", attachments=[("CON.txt", b"reserved"), ('we<ird>|na?me "x".docx', DOCX)],
                     date="Thu, 05 Mar 2026 09:00:00 -0600"),
        inbox / "4.eml",
    )


def _options(**kw) -> Options:
    base = dict(request_id="Request 1", jobs=1, bates=BatesSpec(prefix="T-"), triage=True)
    base.update(kw)
    return Options(**base)


def _index(out: Path) -> list[dict]:
    return list(csv.DictReader(io.StringIO((out / natives.INDEX_CSV_NAME).read_text(encoding="utf-8-sig"))))


def _sha(data: bytes) -> str:
    return Attachment("x", data, "").sha256


def test_triage_end_to_end(tmp_path, recorded_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, _options(terms_file=tmp_path / "ignored.txt", keep_natives=True), tmp_path / "work")

    assert stats["errors"] == 0
    # 9 slots: plan.txt twice, two budgets, the zip and its two files, and two odd names.
    assert stats["attachments"] == 9 and stats["attachments_containers"] == 1
    assert stats["attachments_native"] == 8 and stats["native_files"] == 7
    assert stats["attachments_crossref"] == 0 and stats["attachments_converted"] == 0

    # Files: one per distinct content, named by ID in order of first reference.
    folder = out / natives.NATIVES_DIR_NAME
    assert sorted(p.name for p in folder.iterdir()) == [
        "A0001 plan.txt",
        "A0002 Budget.xlsx",
        "A0003 Budget.xlsx",
        "A0004 a.txt",
        "A0005 b.txt",
        "A0006 CON.txt",
        "A0007 we_ird__na_me _x_.docx",
        "README.txt",
    ]
    assert (folder / "A0001 plan.txt").read_bytes() == b"the plan"
    assert (folder / "A0002 Budget.xlsx").read_bytes() == b"first budget"
    assert (folder / "A0003 Budget.xlsx").read_bytes() == b"second budget"
    assert (folder / "A0004 a.txt").read_bytes() == b"alpha"
    assert (folder / "A0006 CON.txt").is_file()
    assert (folder / "A0007 we_ird__na_me _x_.docx").read_bytes() == DOCX
    assert "must not be released" in (folder / "README.txt").read_text()

    # Triage copies: named so, unnumbered, not pre-marked, no word list, no zip written.
    assert sorted(p.name for p in out.glob("*.pdf")) == ["00 START HERE.pdf", "Triage copy - Volume 01 of 01.pdf"]
    volume = PdfReader(str(out / "Triage copy - Volume 01 of 01.pdf"))
    assert volume.page_labels[0] == "1"  # no Bates labels
    assert "T-0" not in "".join(page.extract_text() for page in volume.pages)
    assert not (out / cli.WORDLIST_NAME).exists()
    assert not any("records.zip" in p.name for p in folder.iterdir())  # an expanded zip is not a file of its own

    # One pointer page per slot, each naming its file.
    pointers = [" ".join(h.split()) for h in recorded_render if "This attachment is a separate file" in h]
    assert len(pointers) == 8
    plan_pages = [p for p in pointers if "A0001" in p]
    assert len(plan_pages) == 2
    assert all("The same file was attached to 2 messages. It is in the folder once." in p for p in plan_pages)
    assert any("named &ldquo;A0003 Budget.xlsx&rdquo;" in p for p in pointers)

    start_here = " ".join(next(h for h in recorded_render if "Start here: triage copy" in h).split())
    assert "Nothing in this folder is for release, and these volumes are not for redaction." in start_here
    assert "7 files" in start_here and natives.INDEX_CSV_NAME in start_here

    # The index: one row per file, every carrier listed, decisions.load-compatible columns.
    rows = _index(out)
    assert [r["id"] for r in rows] == ["A0001", "A0002", "A0003", "A0004", "A0005", "A0006", "A0007"]
    by_id = {r["id"]: r for r in rows}
    plan = by_id["A0001"]
    assert (plan["file"], plan["filename"], plan["type"], plan["copies"], plan["messages"]) == (
        "A0001 plan.txt", "plan.txt", "Plain text", "2", "1; 2",
    )
    assert plan["first_message_subject"] == "Budget" and plan["first_message_date"].startswith("2026-03-02")
    assert plan["last_message_date"].startswith("2026-03-03")
    assert plan["status"] == "file" and plan["decision"] == "" and plan["sha256"] == _sha(b"the plan")
    assert by_id["A0004"]["filename"] == "records.zip/a.txt"
    assert decisions.load(out / natives.INDEX_CSV_NAME) == []  # nothing decided yet

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["options"]["triage"] is True and manifest["options"]["stamp_bates"] is False
    assert manifest["counts"]["native_files"] == 7 and manifest["counts"]["attachments_native"] == 8
    assert manifest["volumes"][0]["filename"] == "Triage copy - Volume 01 of 01.pdf"
    first = manifest["native_files"][0]
    assert (first["id"], first["file"], first["status"], first["messages"]) == ("A0001", "A0001 plan.txt", "file", [1, 2])
    assert "largest_attachments" not in manifest


def test_ids_do_not_move_when_decisions_withhold_files(tmp_path, recorded_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    first = tmp_path / "first"
    cli.build_volumes(extract_root, first, _options(), tmp_path / "w1")
    rows = _index(first)

    # The reviewers mark the first budget omit, in the index itself, and sends it back.
    for r in rows:
        if r["sha256"] == _sha(b"first budget"):
            r["decision"], r["note"] = "omit", "Not responsive to the request"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    marked = tmp_path / "marked.csv"
    marked.write_bytes(buf.getvalue().encode("utf-8-sig"))

    (rule,) = decisions.load(marked)
    assert (rule.decision, rule.sha256, rule.filename) == ("omit", _sha(b"first budget"), "Budget.xlsx")

    second = tmp_path / "second"
    stats = cli.build_volumes(extract_root, second, _options(decisions_file=marked), tmp_path / "w2")
    assert stats["attachments_omitted"] == 1 and stats["native_files"] == 6
    again = _index(second)
    assert [(r["id"], r["sha256"]) for r in again] == [(r["id"], r["sha256"]) for r in rows]
    omitted = next(r for r in again if r["sha256"] == _sha(b"first budget"))
    assert (omitted["id"], omitted["file"], omitted["status"]) == ("A0002", "", "omitted")
    assert (omitted["decision"], omitted["note"], omitted["outcome"]) == ("omit", "Not responsive to the request", "not included (omit)")
    # The other file with the same name is untouched.
    assert (second / natives.NATIVES_DIR_NAME / "A0003 Budget.xlsx").is_file()
    assert not (second / natives.NATIVES_DIR_NAME / "A0002 Budget.xlsx").exists()
    # The index goes back unchanged.
    assert decisions.load(second / natives.INDEX_CSV_NAME) == [
        decisions.Rule(3, "omit", "Budget.xlsx", _sha(b"first budget"), "Not responsive to the request")
    ]


def test_the_production_run_takes_the_index_as_its_decisions(tmp_path, recorded_render):
    extract_root = tmp_path / "extract"
    _tree(extract_root)
    triage = tmp_path / "triage"
    cli.build_volumes(extract_root, triage, _options(), tmp_path / "w1")
    text = (triage / natives.INDEX_CSV_NAME).read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    for r in rows:
        if r["filename"].endswith(".txt"):
            r["decision"], r["note"] = "omit", "Not public data"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    marked = tmp_path / "marked.csv"
    marked.write_bytes(buf.getvalue().encode("utf-8-sig"))

    out = tmp_path / "out"
    before = len(recorded_render)
    opts = Options(request_id="Request 1", jobs=1, bates=BatesSpec(prefix="T-"), convert_attachments=False, decisions_file=marked)
    stats = cli.build_volumes(extract_root, out, opts, tmp_path / "w2")
    # plan.txt on two messages, a.txt, b.txt, CON.txt: five slots, each with its own note.
    assert stats["attachments_omitted"] == 5
    assert (out / "Volume 01 of 01.pdf").is_file() and (out / cli.WORDLIST_NAME).is_file()
    notes = [" ".join(h.split()) for h in recorded_render[before:] if "This attachment is not included" in h]
    assert len(notes) == 5 and all("Not public data." in n for n in notes)


def test_file_names_are_short_safe_and_unique():
    assert natives.file_name("A0042", "Budget FY27.xlsx") == "A0042 Budget FY27.xlsx"
    assert natives.file_name("A0001", "CON") == "A0001 CON"  # reserved only as a whole stem
    assert natives.file_name("A0002", "records.zip/inner/a.txt") == "A0002 a.txt"
    long = natives.file_name("A0003", "x" * 200 + ".xlsx")
    assert len(long) == len("A0003 ") + natives.FILE_NAME_LIMIT and long.endswith(".xlsx")
    assert natives.file_name("A0004", "...") == "A0004 attachment"


def test_ids_widen_past_9999(monkeypatch):
    from types import SimpleNamespace

    from pst2pdf.model import AttachmentEntry, ConvertedAttachment

    monkeypatch.setattr(natives, "_kind", lambda *a: "text")
    bundles = []
    for seq in range(1, 10_002):
        att = Attachment(f"f{seq}.txt", str(seq).encode(), "text/plain")
        conv = ConvertedAttachment(att, "native", "none", None)
        entry = AttachmentEntry(1, 1, att.filename, att.size, att.content_type, att.sha256, conv)
        bundles.append(SimpleNamespace(seq=seq, model=SimpleNamespace(attachments=[att]), attachments=[entry]))
    files = natives.assign_ids(bundles)
    assert files[0].id == "A00001" and files[-1].id == "A10001"
    assert bundles[-1].attachments[0].file_id == "A10001"


# --- the command line ----------------------------------------------------------------


def _fake_run(monkeypatch, seen: dict):
    def fake_extract(pst_path, work_dir):
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def fake_build(extract_root, output_dir, options, work):
        seen["options"] = options
        return {"errors": 0, "messages_found": 0, "duplicates_removed": 0, "messages": 0, "attachments": 0,
                "native_files": 0, "volumes": 1, "volume_pages": 3, "natives_dir": "n", "index_csv": "i.csv"}

    monkeypatch.setattr(cli.extract, "extract_pst", fake_extract)
    monkeypatch.setattr(cli, "build_volumes", fake_build)


def test_cli_triage_implies_its_settings_and_says_what_it_ignores(tmp_path, monkeypatch, capsys):
    pst = tmp_path / "a.pst"
    pst.write_bytes(b"!BDN")
    terms = tmp_path / "terms.txt"
    terms.write_text("[student] Jane Doe\n")
    seen: dict = {}
    _fake_run(monkeypatch, seen)
    code = cli.main([str(pst), str(tmp_path / "out"), "--triage", "--terms", str(terms), "--no-dedup-attachments", "--keep-natives"])
    assert code == 0
    err, out = capsys.readouterr()[::-1]
    assert "--terms is ignored" in err and "--no-dedup-attachments is ignored" in err
    assert "--keep-natives" not in err  # implied, so nothing to say
    assert "Attachment index for review: i.csv" in out
    opts = seen["options"]
    assert opts.triage and not opts.stamp_bates and not opts.convert_attachments and opts.dedup_attachments
    assert opts.terms_file is None and not opts.keep_natives


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--survey"], "--triage and --survey are different runs"),
        (["--appendix-over", "5"], "--appendix-over cannot be combined with --triage"),
    ],
)
def test_cli_triage_refuses_what_it_cannot_do(tmp_path, capsys, extra, message):
    pst = tmp_path / "a.pst"
    pst.write_bytes(b"!BDN")
    with pytest.raises(SystemExit) as exc:
        cli.main([str(pst), str(tmp_path / "out"), "--triage", *extra])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
