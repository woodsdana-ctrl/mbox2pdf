"""--decisions: the reviewers' per-attachment decisions, read from the survey CSV."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pst2pdf import cli, decisions, render, survey
from pst2pdf.model import Attachment, BatesSpec, Options

from conftest import make_message, write_eml
from test_volumes import _fake_render_pdf

CENTRAL = timezone(timedelta(hours=-5))


def _csv(path: Path, rows: list[dict], columns=survey.CSV_COLUMNS, encoding="utf-8-sig", delimiter=",") -> Path:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), delimiter=delimiter, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in columns})
    path.write_bytes(buf.getvalue().encode(encoding))
    return path


def _entries(*messages):
    """(date, [(filename, bytes)]) per message, as organize would hand them on.
    Message N's subject is "Report N"."""
    return [
        SimpleNamespace(
            model=SimpleNamespace(
                date=date, subject=f"Report {seq}", attachments=[Attachment(n, b, "text/plain") for n, b in atts]
            )
        )
        for seq, (date, atts) in enumerate(messages, start=1)
    ]


def _day(n: int) -> datetime:
    return datetime(2026, 3, 1, 7, 30, tzinfo=CENTRAL) + timedelta(days=n)


# --- reading the file ---------------------------------------------------------------


def test_load_reads_only_rows_with_a_decision(tmp_path):
    path = _csv(
        tmp_path / "d.csv",
        [
            {"filename": "Daily summary.XLS", "sha256": "AB" * 32, "decision": "Latest", "note": "daily export"},
            {"filename": "notes.txt", "sha256": "cd" * 32},  # no decision: ignored
            {"filename": "DailyReport_*.xlsx", "decision": "latest"},
            {"sha256": "ef" * 32, "decision": "produce"},
        ],
    )
    rules = decisions.load(path)
    assert [(r.decision, r.filename, r.sha256, r.note) for r in rules] == [
        ("latest", "Daily summary.XLS", "ab" * 32, "daily export"),
        ("latest", "DailyReport_*.xlsx", "", ""),
        ("produce", "", "ef" * 32, ""),
    ]
    assert [r.line for r in rules] == [2, 4, 5]
    assert rules[1].pattern and not rules[0].pattern


def test_load_reads_excel_unicode_text(tmp_path):
    """Excel's "Unicode Text" save is tab-separated UTF-16."""
    path = _csv(tmp_path / "d.txt", [{"filename": "a.xls", "decision": "latest"}], encoding="utf-16", delimiter="\t")
    assert [(r.filename, r.decision) for r in decisions.load(path)] == [("a.xls", "latest")]


@pytest.mark.parametrize(
    "row, message",
    [
        ({"filename": "a.xls", "decision": "keep newest"}, "unknown decision 'keep newest'"),
        ({"decision": "latest"}, "no filename or sha256"),
    ],
)
def test_load_rejects_bad_rows_with_their_line(tmp_path, row, message):
    path = _csv(tmp_path / "d.csv", [{"filename": "ok.txt"}, row])
    with pytest.raises(ValueError, match=f"d.csv:3: .*{message}"):
        decisions.load(path)


def test_load_needs_a_decision_column(tmp_path):
    path = _csv(tmp_path / "d.csv", [{"filename": "a"}], columns=("filename", "sha256"))
    with pytest.raises(ValueError, match="no 'decision' column"):
        decisions.load(path)


# --- applying the rules ---------------------------------------------------------------


def test_latest_keeps_the_copy_from_the_most_recent_message():
    entries = _entries(
        (_day(2), [("Daily summary.XLS", b"v2")]),
        (_day(0), [("Daily summary.XLS", b"v0")]),
        (_day(5), [("daily summary.xls", b"v5")]),  # case differs: same report
        (_day(1), [("Daily summary.XLS", b"v1"), ("other.txt", b"x")]),
        (_day(3), [("Daily summary.XLS", b"v5")]),  # the latest bytes, sent earlier too
    )
    rules = [decisions.Rule(2, "latest", "Daily summary.XLS", "")]
    outcome = decisions.apply(entries, rules)
    v = {name: Attachment("x", name.encode(), "").sha256 for name in ("v0", "v1", "v2", "v5", "x")}
    assert set(outcome.withheld) == {v["v0"], v["v1"], v["v2"]}
    assert outcome.kept == {2: [v["v5"]]}
    assert outcome.matched == {2: 5}
    w = outcome.withheld[v["v0"]]
    assert (w.copies, w.kept_seq, w.kept_date) == (4, 3, _day(5))  # message 3 is the first to show v5 in full
    # A count of copies on a released page says how much was collected.
    assert "an earlier copy of the recurring report “daily summary.xls”" in w.reason()
    assert "4" not in w.reason()
    # The note page is released, so it names the message by date and subject;
    # the exceptions table below keeps the number.
    assert "only the latest is included, attached to the message of March 6, 2026, “Report 3”" in w.reason()
    assert "message 3" not in w.reason()
    assert w.reason().startswith("it was withheld at the reviewer's direction: it is an earlier copy")
    assert w.reason("the records custodian").startswith("it was withheld at the direction of the records custodian: it is an earlier copy")
    assert w.summary() == (
        "Withheld: an earlier copy of a recurring report (4 copies); the latest is attached to message 3 (March 6, 2026)"
    )


def test_a_pattern_covers_names_that_change_daily_and_files_from_zips():
    entries = _entries(
        (_day(0), [("DailyReport_2026-03-01.xlsx", b"a")]),
        (_day(1), [("DailyReport_2026-03-02.xlsx", b"b")]),
        (_day(2), [("export.zip/DailyReport_2026-03-03.xlsx", b"c")]),
        (_day(3), [("Weekly report.xlsx", b"d")]),
    )
    outcome = decisions.apply(entries, [decisions.Rule(9, "latest", "dailyreport_*.XLSX", "", "superseded daily")])
    sha = {k: Attachment("x", k, "").sha256 for k in (b"a", b"b", b"c", b"d")}
    assert set(outcome.withheld) == {sha[b"a"], sha[b"b"]}
    assert outcome.kept[9] == [sha[b"c"]]
    assert outcome.withheld[sha[b"a"]].reason().endswith("Note: superseded daily")


def test_a_sha256_row_groups_by_that_attachments_filename_and_produce_exempts():
    entries = _entries(
        (_day(0), [("roster.xls", b"a")]),
        (_day(1), [("roster.xls", b"b")]),
        (_day(2), [("roster.xls", b"c")]),
        (_day(3), [("other.xls", b"z")]),
    )
    sha = {k: Attachment("x", k, "").sha256 for k in (b"a", b"b", b"c")}
    rules = [decisions.Rule(2, "latest", "", sha[b"b"]), decisions.Rule(3, "produce", "", sha[b"a"])]
    outcome = decisions.apply(entries, rules)
    assert set(outcome.withheld) == {sha[b"b"]}  # a is exempt, c is the latest
    assert outcome.unmatched(rules) == []


def test_a_rule_that_matches_nothing_is_reported():
    rules = [decisions.Rule(4, "latest", "Staf Directory*.xls", "")]
    outcome = decisions.apply(_entries((_day(0), [("Daily summary.xls", b"a")])), rules)
    assert outcome.withheld == {} and outcome.unmatched(rules) == rules


def test_undated_copies_count_as_oldest():
    entries = _entries((None, [("r.xls", b"a")]), (_day(0), [("r.xls", b"b")]))
    outcome = decisions.apply(entries, [decisions.Rule(2, "latest", "r.xls", "")])
    assert list(outcome.withheld) == [Attachment("x", b"a", "").sha256]


# --- end to end -------------------------------------------------------------------------


@pytest.fixture
def recorded_render(monkeypatch):
    seen: list[str] = []

    def record(html: str, out_path: Path) -> int:
        seen.append(html)
        return _fake_render_pdf(html, out_path)

    monkeypatch.setattr(render, "render_pdf", record)
    return seen


def _report_tree(root: Path) -> None:
    """Five daily copies of one report (the last one sent twice), and a note."""
    inbox = root / "Inbox"
    inbox.mkdir(parents=True)
    for day in range(5):
        body = "\n".join(f"{1000 + i}\tDOE\tJANE\tday {day}" for i in range(3)).encode()
        write_eml(
            make_message(
                subject=f"Daily summary {day}",
                attachments=[("Daily summary.xls", body)],
                date=f"Mon, {day + 2:02d} Mar 2026 07:30:00 -0600",
            ),
            inbox / f"{day}.eml",
        )
    latest = "\n".join(f"{1000 + i}\tDOE\tJANE\tday 4" for i in range(3)).encode()
    write_eml(
        make_message(subject="Fwd: summary", attachments=[("Daily summary.xls", latest), ("notes.txt", b"hi")],
                     date="Mon, 09 Mar 2026 09:00:00 -0600"),
        inbox / "9.eml",
    )


def test_decisions_end_to_end(tmp_path, recorded_render, monkeypatch):
    extract_root = tmp_path / "extract"
    _report_tree(extract_root)
    rules_file = _csv(tmp_path / "decisions.csv", [{"filename": "Daily summary.xls", "decision": "latest", "note": "daily export."}])
    from pst2pdf import convert

    converted: list[str] = []
    real_submit = convert.Converter.submit
    monkeypatch.setattr(convert.Converter, "submit", lambda self, att: converted.append(att.filename) or real_submit(self, att))
    opts = Options(request_id="Request 1", jobs=1, bates=BatesSpec(prefix="T-"), decisions_file=rules_file)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, opts, tmp_path / "work")

    assert stats["errors"] == 0
    assert stats["attachments"] == 7
    assert stats["attachments_withheld"] == 4  # days 0-3; day 4 is produced, its forward cross-referenced
    assert stats["attachments_crossref"] == 1 and stats["attachments_converted"] == 2
    assert sorted(converted) == ["Daily summary.xls", "notes.txt"]  # withheld copies never converted

    notes = [h for h in recorded_render if "This attachment was withheld" in h]
    assert len(notes) == 4
    assert all("it is an earlier copy of the recurring report" in " ".join(n.split()) for n in notes)
    assert not any("5 copies" in n for n in notes)
    assert all("Note: daily export.</p>" in n for n in notes)  # the note's own period, not a second one
    assert not any("<th>Pages</th>" in n for n in notes)  # never converted: no count to give
    exceptions = " ".join(next(h for h in recorded_render if "Exceptions, page" in h).split())
    assert "4 attachments withheld at the reviewer" in exceptions  # no --withheld-by: the neutral wording
    # One row for the report, not one per copy; the manifest keeps them all.
    assert exceptions.count("Daily summary.xls") == 1
    assert (
        "Withheld: 4 attachments, earlier copies of a recurring report, each replaced by a note where it was attached "
        "(messages 1 to 4); the latest is attached to message 5 (March 6, 2026)" in exceptions
    )
    start_here = next(h for h in recorded_render if "Start here" in h and "What this run found" in h)
    assert "4 were earlier copies of a recurring report" in " ".join(start_here.split())

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"]["attachments_withheld"] == 4
    assert manifest["options"]["decisions_file"] is True
    (rule,) = manifest["decisions"]["rules"]
    assert (rule["line"], rule["decision"], rule["attachments_matched"]) == (2, "latest", 6)
    assert len(rule["withheld_sha256"]) == 4 and len(rule["kept_sha256"]) == 1
    withheld_rows = [e for e in manifest["exceptions"] if e["reason"].startswith("Withheld")]
    assert len(withheld_rows) == 4
    # One line each in the table; the full wording and the reviewers' note are on the note page.
    # Message 5 (March 6) shows the latest bytes in full; message 6 forwards
    # the same file on March 9 and cross-references it.
    assert withheld_rows[0]["reason"] == (
        "Withheld: an earlier copy of a recurring report (5 copies); the latest is attached to message 5 (March 6, 2026)"
    )


def test_survey_csv_carries_decisions_forward(tmp_path, recorded_render):
    extract_root = tmp_path / "extract"
    _report_tree(extract_root)
    sha_latest = Attachment("x", "\n".join(f"{1000 + i}\tDOE\tJANE\tday 4" for i in range(3)).encode(), "").sha256
    rules_file = _csv(
        tmp_path / "decisions.csv",
        [
            {"filename": "Daily summary.xls", "sha256": sha_latest, "decision": "latest"},
            {"filename": "DailyReport_*.xlsx", "decision": "latest", "note": "hand-added pattern"},
        ],
    )
    opts = Options(request_id="Request 1", jobs=1, survey=True, decisions_file=rules_file)
    stats = cli.build_volumes(extract_root, tmp_path / "out", opts, tmp_path / "work")
    rows = list(csv.DictReader(io.StringIO((tmp_path / "out" / survey.SURVEY_CSV_NAME).read_text(encoding="utf-8-sig"))))
    by_sha = {r["sha256"]: r for r in rows if r["sha256"]}
    assert by_sha[sha_latest]["decision"] == "latest"
    assert by_sha[sha_latest]["outcome"] == "produced: the latest of 5 copies"
    withheld = [r for r in rows if r["outcome"].startswith("withheld")]
    assert len(withheld) == 4 and all(r["type"] == "Withheld by decision" and r["pages"] == "0" for r in withheld)
    assert rows[-1]["filename"] == "DailyReport_*.xlsx" and rows[-1]["note"] == "hand-added pattern"
    # Fed back unchanged, the same rules come out.
    again = decisions.load(tmp_path / "out" / survey.SURVEY_CSV_NAME)
    assert [(r.decision, r.filename, r.sha256) for r in again] == [
        ("latest", "Daily summary.xls", sha_latest),
        ("latest", "DailyReport_*.xlsx", ""),
    ]
    assert stats["page_budget"].attachment_pages < 10


def test_cli_rejects_a_bad_decisions_file_before_converting(tmp_path, capsys):
    pst = tmp_path / "a.pst"
    pst.write_bytes(b"!BDN")
    bad = _csv(tmp_path / "d.csv", [{"filename": "a.xls", "decision": "oldest"}])
    with pytest.raises(SystemExit):
        cli.main([str(pst), str(tmp_path / "out"), "--decisions", str(bad)])
    assert "d.csv:2: unknown decision 'oldest'" in capsys.readouterr().err


def test_a_report_with_one_withheld_copy_reads_in_the_singular_and_keeps_its_size():
    from pst2pdf.report import ExceptionRow, group_withheld

    reason = "Withheld: an earlier copy of a recurring report (2 copies); the latest is attached to message 15 (March 3, 2026)"
    row = ExceptionRow(12, 1, "Weekly summary.pdf", 150000, "a" * 64, "application/pdf", reason, "", "", None, "Weekly summary", withheld=True)
    (grouped,) = group_withheld([row])
    assert (grouped.count, grouped.size) == (1, 150000)
    assert grouped.reason == (
        "Withheld: an earlier copy of a recurring report, replaced by a note where it was attached (message 12); "
        "the latest is attached to message 15 (March 3, 2026)"
    )


# --- omit -------------------------------------------------------------------------------


def test_load_needs_a_note_on_an_omit_row(tmp_path):
    path = _csv(tmp_path / "d.csv", [{"filename": "a.log", "sha256": "ab" * 32, "decision": "Omit"}])
    with pytest.raises(ValueError, match="d.csv:2: an omit row needs a note"):
        decisions.load(path)
    path = _csv(tmp_path / "d.csv", [{"filename": "a.log", "decision": "omit", "note": "Not responsive"}])
    assert [(r.decision, r.note) for r in decisions.load(path)] == [("omit", "Not responsive")]


def test_the_unknown_decision_message_lists_omit(tmp_path):
    path = _csv(tmp_path / "d.csv", [{"filename": "a.xls", "decision": "drop"}])
    with pytest.raises(ValueError, match="use latest, omit or produce"):
        decisions.load(path)


def test_omit_by_sha256_leaves_a_different_file_with_the_same_name():
    entries = _entries(
        (_day(0), [("Budget.xlsx", b"first budget")]),
        (_day(1), [("Budget.xlsx", b"second budget"), ("Budget.xlsx", b"first budget")]),
    )
    first = Attachment("x", b"first budget", "").sha256
    rules = [decisions.Rule(2, "omit", "Budget.xlsx", first, "Not responsive to the request")]
    outcome = decisions.apply(entries, rules)
    assert set(outcome.withheld) == {first}
    assert outcome.matched == {2: 2}  # both slots that carried those bytes
    assert outcome.kept == {}
    why = outcome.withheld[first]
    assert why.reason() == "Not responsive to the request."
    assert why.summary() == "Not included: Not responsive to the request."
    assert why.outcome() == "not included (omit)"


def test_omit_by_pattern_covers_every_matching_name_and_produce_exempts():
    entries = _entries(
        (_day(0), [("sync-0301.log", b"a"), ("notes.txt", b"n")]),
        (_day(1), [("sync-0302.log", b"b")]),
        (_day(2), [("jobs.zip/sync-0303.log", b"c")]),
    )
    sha = {k: Attachment("x", k, "").sha256 for k in (b"a", b"b", b"c", b"n")}
    rules = [
        decisions.Rule(2, "omit", "sync-*.log", "", "System logs, not public data."),
        decisions.Rule(3, "produce", "", sha[b"b"]),
    ]
    outcome = decisions.apply(entries, rules)
    assert set(outcome.withheld) == {sha[b"a"], sha[b"c"]}
    assert outcome.withheld[sha[b"a"]].reason() == "System logs, not public data."  # the note's own full stop, not a second


def test_an_earlier_rule_stands_between_omit_and_latest():
    entries = _entries(
        (_day(0), [("Daily.xls", b"d0")]),
        (_day(1), [("Daily.xls", b"d1")]),
        (_day(2), [("Daily.xls", b"d2")]),
    )
    d0 = Attachment("x", b"d0", "").sha256
    rules = [decisions.Rule(2, "omit", "", d0, "Out of scope"), decisions.Rule(3, "latest", "Daily.xls", "")]
    outcome = decisions.apply(entries, rules)
    assert isinstance(outcome.withheld[d0], decisions.Omitted)
    assert [type(w).__name__ for s, w in outcome.withheld.items() if s != d0] == ["Withheld"]
    assert outcome.withheld[Attachment("x", b"d1", "").sha256].copies == 2  # the omitted copy is not one of the report's


def test_omit_end_to_end(tmp_path, recorded_render, monkeypatch):
    extract_root = tmp_path / "extract"
    _report_tree(extract_root)
    rules_file = _csv(
        tmp_path / "decisions.csv",
        [{"filename": "Daily summary.xls", "decision": "omit", "note": "Not responsive to the request"}],
    )
    from pst2pdf import convert

    converted: list[str] = []
    real_submit = convert.Converter.submit
    monkeypatch.setattr(convert.Converter, "submit", lambda self, att: converted.append(att.filename) or real_submit(self, att))
    opts = Options(request_id="Request 1", jobs=1, bates=BatesSpec(prefix="T-"), decisions_file=rules_file)
    out = tmp_path / "out"
    stats = cli.build_volumes(extract_root, out, opts, tmp_path / "work")

    assert stats["errors"] == 0
    # Five distinct copies on six messages; the forward's copy gets its own note, not a cross-reference.
    assert stats["attachments_omitted"] == 6 and stats["attachments_withheld"] == 0
    assert stats["attachments_crossref"] == 0 and stats["attachments_converted"] == 1
    assert converted == ["notes.txt"]

    notes = [" ".join(h.split()) for h in recorded_render if "This attachment is not included" in h]
    assert len(notes) == 6
    assert all("<strong>Not included.</strong> Not responsive to the request.</p>" in n for n in notes)
    assert not any("at the direction" in n or "because" in n for n in notes)
    exceptions = " ".join(next(h for h in recorded_render if "Exceptions, page" in h).split())
    assert "6 attachments not included" in exceptions and "withheld at the direction" not in exceptions
    assert exceptions.count("Daily summary.xls") == 1
    assert (
        "Not included: 6 attachments (messages 1 to 6), each replaced by a note where it was attached. "
        "Not responsive to the request." in exceptions
    )
    start_here = " ".join(next(h for h in recorded_render if "Start here" in h and "What this run found" in h).split())
    assert "6 were not included as you directed. Each has a one-page note in its place giving your reason" in start_here

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"]["attachments_omitted"] == 6
    omitted = [e for e in manifest["exceptions"] if e["decision"] == "omit"]
    assert len(omitted) == 6 and omitted[0]["reason"] == "Not included: Not responsive to the request."
    (rule,) = manifest["decisions"]["rules"]
    assert (rule["decision"], rule["attachments_matched"], rule["kept_sha256"]) == ("omit", 6, [])
    assert len(rule["withheld_sha256"]) == 5


def test_a_single_omitted_attachment_keeps_its_row_as_it_is():
    from pst2pdf.report import ExceptionRow, group_withheld

    row = ExceptionRow(4, 1, "a.log", 99, "a" * 64, "text/plain", "Not included: Out of scope.", "", "", None, "Logs",
                       withheld=True, decision="omit")
    (grouped,) = group_withheld([row])
    assert grouped == row
