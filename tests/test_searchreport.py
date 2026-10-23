"""--search-report: the Purview query on START HERE and in the manifest.

The Summary CSV below copies the layout of a real Purview export report
(Name/Value/Description rows, blank rows and one-cell headings between
them, a byte-order mark, CRLF) with every value made up.
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from datetime import timedelta
from html import unescape
from pathlib import Path

import pytest

from pst2pdf import cli, report, searchreport, survey
from pst2pdf.model import BatesSpec, Options

QUERY = '(("budget memo") (c:s) (quarterly review)) (c:c) (Date=2020-01-01..2020-06-30)'
SUBMITTER = "operator@example.org"


def summary_csv(
    query: str | None = QUERY,
    *,
    security: str = "No",
    location: str = "",
    content: str = "",
    started: str = "3/4/2021 1:02:03 PM",
    finished: str = "3/4/2021 2:03:04 PM",
) -> bytes:
    rows = [
        '"Help with this report"',
        '"https://example.org/help"',
        "",
        '"Job ID","0123456789abcdef0123456789abcdef","A unique identifier."',
        '"Case Name","Example case 12","The name assigned to the case."',
        '"Search Name","Example search","The name given to the search."',
        '"Export Name","Example export","The name assigned to the export."',
        "",
    ]
    if query is not None:
        rows.append('"Query","' + query.replace('"', '""') + '"')
    rows += [
        f'"StartTime (UTC)","{started}"',
        f'"EndTime (UTC)","{finished}"',
        f'"SubmittedBySmtp","{SUBMITTER}"',
        "",
        '"Security Filter"',
        f'"Security Filter Applied","{security}","Whether ""Yes"" or ""No""."',
        f'"Location Restriction","{location}","The filters applied."',
        f'"Content Restriction","{content}","The content filter."',
        "",
        '"Name","Item Count","Description"',
        '"Search results","","Summary of items."',
        '"Indexed items","12","Items that matched."',
        '"Total","12","Total number of items exported."',
        "",
    ]
    return ("﻿" + "\r\n".join(rows) + "\r\n").encode("utf-8")


def _text(html: str) -> str:
    html = re.sub(r"<style>.*?</style>", " ", html, flags=re.DOTALL)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _opts(**kw) -> Options:
    base = dict(request_id="Request 1", bates=BatesSpec(prefix="T-", start=1))
    base.update(kw)
    return Options(**base)


@pytest.fixture
def local_zone(monkeypatch):
    """Set the container's local zone (START HERE shows local times)."""

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def summary(tmp_path) -> Path:
    path = tmp_path / "Summary-2021-03-04_13-02-03.csv"
    path.write_bytes(summary_csv())
    return path


# --- reading ----------------------------------------------------------------


def test_reads_the_named_rows_of_a_summary_csv(summary):
    got = searchreport.load(summary)
    assert got.source == "report"
    assert got.query == QUERY
    assert (got.case_name, got.search_name, got.export_name) == ("Example case 12", "Example search", "Example export")
    assert got.job_id == "0123456789abcdef0123456789abcdef"
    assert (got.started, got.finished) == ("3/4/2021 1:02:03 PM", "3/4/2021 2:03:04 PM")
    assert got.security_filter == "No"
    assert len(got.sha256) == 64


def test_never_keeps_the_submitters_address(summary):
    got = searchreport.load(summary)
    assert SUBMITTER not in json.dumps(searchreport.manifest_entry(got))
    assert SUBMITTER not in repr(got)


def test_reads_the_summary_from_the_reports_zip_and_ignores_the_other_reports(tmp_path):
    zpath = tmp_path / "Reports-Example-Export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("Items_0_2021-03-04_13-02-03.csv", '"Query","not this one"\r\n')
        zf.writestr("Locations-2021-03-04_13-02-03.csv", "")
        zf.writestr("Summary-2021-03-04_13-02-03.csv", summary_csv())
    assert searchreport.load(zpath).query == QUERY


def test_reads_the_summary_from_an_unzipped_reports_folder(tmp_path):
    folder = tmp_path / "Reports-Example-Export"
    folder.mkdir()
    (folder / "Settings-2021-03-04_13-02-03.csv").write_text("")
    (folder / "Summary-2021-03-04_13-02-03.csv").write_bytes(summary_csv())
    assert searchreport.load(folder).query == QUERY


@pytest.mark.parametrize("names", [[], ["Summary-1.csv", "Summary-2.csv"]])
def test_a_zip_or_folder_needs_exactly_one_summary(tmp_path, names):
    zpath = tmp_path / "Reports.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("Items_0.csv", "")
        for n in names:
            zf.writestr(n, summary_csv())
    with pytest.raises(ValueError, match="Summary"):
        searchreport.load(zpath)


def test_a_csv_without_a_query_row_is_refused(tmp_path):
    path = tmp_path / "Items_0.csv"
    path.write_bytes(summary_csv(query=None))
    with pytest.raises(ValueError, match="no Query row"):
        searchreport.load(path)


def test_an_empty_query_is_refused(tmp_path):
    path = tmp_path / "Summary-1.csv"
    path.write_bytes(summary_csv(query=""))
    with pytest.raises(ValueError, match="empty"):
        searchreport.load(path)


def test_a_text_file_is_the_query_alone(tmp_path):
    path = tmp_path / "query.txt"
    path.write_text("﻿subject:“budget” AND sent>=2020-01-01\r\n", encoding="utf-8")
    got = searchreport.load(path)
    assert got.source == "text"
    assert got.query == "subject:“budget” AND sent>=2020-01-01"  # curly quotes kept as given
    assert got.case_name == ""
    (tmp_path / "blank.txt").write_text("  \n")
    with pytest.raises(ValueError, match="empty"):
        searchreport.load(tmp_path / "blank.txt")


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        searchreport.load(tmp_path / "nope.zip")


def test_times_are_read_as_utc():
    dt = searchreport.parse_time("3/4/2021 1:02:03 PM")
    assert (dt.year, dt.month, dt.day, dt.hour, dt.utcoffset().total_seconds()) == (2021, 3, 4, 13, 0)
    assert searchreport.parse_time("sometime") is None


# --- START HERE, the survey, the manifest ----------------------------------------


def test_start_here_shows_the_query_exactly(summary, local_zone):
    local_zone("UTC")
    opts = _opts(search_report=searchreport.load(summary))
    html = report.build_start_here_html(opts, {}, None)
    text = _text(html)
    assert "The search" in text
    assert QUERY in unescape(html)  # not reflowed, requoted or cut
    assert "Case Example case 12" in text and "Search Example search" in text
    assert "(c:s) separates keywords entered on separate lines" in text
    assert "everything after (c:c) is a condition every item also had to meet" in text
    assert "permissions filter" not in text.lower()
    section = text.split("The search")[1].split("What this run found")[0]
    assert "Exported March 4, 2021, 1:02:03 PM to 2:03:04 PM UTC" in section
    assert "Run time 1 hour, 1 minute, 1 second" in section
    assert "does not include any earlier statistics or sample runs" in section
    for absent in ("0123456789abcdef", "Indexed", "Total", SUBMITTER):  # job ID, the export's counts, who ran it
        assert absent not in section


def test_start_here_without_a_report_has_no_search_section():
    assert "The search" not in _text(report.build_start_here_html(_opts(), {}, None))


def test_the_notation_note_only_names_what_the_query_uses(tmp_path):
    path = tmp_path / "q.txt"
    path.write_text("budget AND memo")
    text = _text(report.build_start_here_html(_opts(search_report=searchreport.load(path)), {}, None))
    assert "notation" not in text
    assert "supplied as text with this run" in text


def test_start_here_says_when_a_permissions_filter_was_applied(tmp_path):
    path = tmp_path / "Summary-1.csv"
    path.write_bytes(summary_csv(security="Yes", location="Department -eq 'Example'"))
    text = _text(report.build_start_here_html(_opts(search_report=searchreport.load(path)), {}, None))
    assert "Permissions filter Applied. Locations: Department -eq 'Example'" in text
    assert "limited what this search could reach" in text


def test_a_query_of_exactly_255_characters_is_flagged(tmp_path):
    path = tmp_path / "Summary-1.csv"
    path.write_bytes(summary_csv(query="x" * searchreport.TRUNCATED_LENGTH))
    text = _text(report.build_start_here_html(_opts(search_report=searchreport.load(path)), {}, None))
    assert "exactly 255 characters" in text


def test_triage_start_here_and_the_survey_show_it_too(summary):
    opts = _opts(search_report=searchreport.load(summary))
    assert QUERY in unescape(report.build_start_here_triage_html(opts, {}, None, []))
    budget = survey.measure([], opts, None, front_matter_pages=1)
    assert QUERY in unescape(survey.build_survey_html(opts, budget))


def test_manifest_records_the_query_and_the_reports_hash(tmp_path, summary):
    got = searchreport.load(summary)
    doc = json.loads(report.write_manifest(tmp_path, _opts(search_report=got), {}, None, []).read_text())
    block = doc["search_report"]
    assert block["query"] == QUERY and block["source"] == "report"
    assert block["sha256"] == got.sha256
    assert block["started_utc"] == "2021-03-04T13:02:03+00:00"
    assert block["run_time_seconds"] == 3661
    assert doc["options"]["search_report"] is True
    assert SUBMITTER not in json.dumps(doc)


def test_manifest_without_a_report_has_no_block(tmp_path):
    doc = json.loads(report.write_manifest(tmp_path, _opts(), {}, None, []).read_text())
    assert "search_report" not in doc and doc["options"]["search_report"] is False


# --- the CLI ----------------------------------------------------------------


def test_cli_refuses_a_bad_report_before_extracting(tmp_path, monkeypatch):
    pst = tmp_path / "a.pst"
    pst.write_bytes(b"!BDN")
    bad = tmp_path / "Items_0.csv"
    bad.write_bytes(summary_csv(query=None))
    monkeypatch.setattr(cli.extract, "extract_pst", lambda *a, **k: pytest.fail("extracted before checking the report"))
    with pytest.raises(SystemExit) as exc:
        cli.main([str(pst), str(tmp_path / "out"), "--search-report", str(bad)])
    assert exc.value.code == 2


def test_cli_help_names_the_option():
    assert "--search-report" in cli.build_parser().format_help()


# --- run time ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0 seconds"),
        (45, "45 seconds"),
        (3600, "1 hour"),
        (5025, "1 hour, 23 minutes, 45 seconds"),
        (26 * 3600 + 185, "26 hours, 3 minutes, 5 seconds"),  # never rolled into days, never rounded
    ],
)
def test_duration_text(seconds, text):
    assert searchreport.duration_text(timedelta(seconds=seconds)) == text


def _start_here(tmp_path, **times) -> str:
    path = tmp_path / "Summary-1.csv"
    path.write_bytes(summary_csv(**times))
    return _text(report.build_start_here_html(_opts(search_report=searchreport.load(path)), {}, None))


def test_run_time_across_midnight_in_the_local_zone(tmp_path, local_zone):
    local_zone("America/Chicago")
    text = _start_here(tmp_path, started="3/5/2021 5:30:00 AM", finished="3/5/2021 6:45:30 AM")
    assert "Exported March 4, 2021, 11:30:00 PM CST to March 5, 2021, 12:45:30 AM CST" in text
    assert "Run time 1 hour, 15 minutes, 30 seconds" in text


@pytest.mark.parametrize("finished", ["", "3/4/2021 1:00:00 PM", "soon"])  # missing, before the start, unreadable
def test_no_run_time_without_a_usable_end(tmp_path, local_zone, finished):
    """A missing or unreadable end, or one before the start, gives no run time
    and no note about it, rather than a wrong figure."""
    local_zone("UTC")
    text = _start_here(tmp_path, finished=finished)
    assert "Exported March 4, 2021 " in text  # the day alone
    assert "Run time" not in text
    path = tmp_path / "Summary-1.csv"
    assert searchreport.manifest_entry(searchreport.load(path))["run_time_seconds"] is None
