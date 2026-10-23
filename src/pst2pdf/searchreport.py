"""The Purview search behind the archive (``--search-report``).

Microsoft Purview writes a process report for every export, downloaded from
the export's Process manager as ``Reports-<case>-<search>-<process>-<export>-
<timestamp>.zip`` (Safari unzips it into a folder of the same name). Its
``Summary-<timestamp>.csv`` records the query the export ran, as Purview
compiled it from the condition builder, so START HERE can show the query
without anyone retyping or pasting it.

The Summary is not a table. It is rows of ``"Name","Value","Description"``
with blank rows and one-cell section headings between them, UTF-8 with a
byte-order mark and CRLF line endings. Only the named rows below are read;
the first occurrence of a name wins. The submitter's address is never read,
and neither are the item counts: one export often becomes several .pst files,
so the export's total would not match any one archive.

A plain text file is accepted too, holding nothing but the query, for a
search whose report is not available. START HERE says so.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .model import SearchReport

#: Microsoft notes that some report options cut every string field to its
#: first 255 characters. A query of exactly that length may have lost its end.
TRUNCATED_LENGTH = 255

_SUMMARY_NAME = re.compile(r"^summary[-_].*\.csv$", re.IGNORECASE)

#: Normalized row name -> SearchReport field.
_ROWS = {
    "jobid": "job_id",
    "casename": "case_name",
    "searchname": "search_name",
    "exportname": "export_name",
    "query": "query",
    "starttime": "started",
    "starttimeutc": "started",
    "endtime": "finished",
    "endtimeutc": "finished",
    "securityfilterapplied": "security_filter",
    "locationrestriction": "location_restriction",
    "contentrestriction": "content_restriction",
}

_TIME_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def is_summary_name(name: str) -> bool:
    return bool(_SUMMARY_NAME.match(Path(name).name))


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _one(names: list[str], where: str) -> str:
    if not names:
        raise ValueError(f"no Summary-….csv in {where}: pass the Reports zip (or folder) of the Purview export, or its Summary CSV")
    if len(names) > 1:
        raise ValueError(f"more than one Summary CSV in {where}: pass the one for this export")
    return names[0]


def _summary_bytes(path: Path) -> bytes:
    if path.is_dir():
        found = sorted(p.name for p in path.iterdir() if p.is_file() and is_summary_name(p.name))
        return (path / _one(found, str(path))).read_bytes()
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                found = sorted(n for n in zf.namelist() if not n.endswith("/") and is_summary_name(n))
                return zf.read(_one(found, path.name))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{path.name} is not a readable zip: {exc}") from exc
    return path.read_bytes()


def parse_summary(data: bytes) -> dict[str, str]:
    """The named rows of a Purview Summary CSV, as SearchReport field -> value."""
    found: dict[str, str] = {}
    for row in csv.reader(io.StringIO(_decode(data))):
        if len(row) < 2:
            continue
        field = _ROWS.get(_key(row[0]))
        if field and field not in found:
            found[field] = row[1].strip()
    return found


def load(path: Path) -> SearchReport:
    """Read *path*: a Reports zip, a folder it was unzipped into, a Summary
    CSV, or a text file holding only the query. Raises ValueError when there
    is no query to show."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"search report not found: {path}")
    looks_like_report = path.is_dir() or zipfile.is_zipfile(path) or path.suffix.lower() == ".csv"
    if not looks_like_report:
        data = path.read_bytes()
        query = _decode(data).strip()
        if not query:
            raise ValueError(f"{path.name} is empty: it should hold the query that was run")
        return SearchReport(query=query, source="text", sha256=hashlib.sha256(data).hexdigest())
    data = _summary_bytes(path)
    fields = parse_summary(data)
    if "query" not in fields:
        raise ValueError(f"{path.name} has no Query row: is it the Summary report of the Purview export?")
    if not fields["query"]:
        raise ValueError(f"the Query row in {path.name} is empty")
    return SearchReport(source="report", sha256=hashlib.sha256(data).hexdigest(), **fields)


def maybe_truncated(report: SearchReport) -> bool:
    return len(report.query) == TRUNCATED_LENGTH


def parse_time(text: str) -> datetime | None:
    """A Summary time (UTC, as written), or None when it is not in a known form."""
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def run_time(report: SearchReport) -> timedelta | None:
    """How long the export process ran, from Purview's StartTime to its
    EndTime, or None when either is missing or unreadable or they are out of
    order. It covers that one process only: not an earlier Generate
    statistics or sample run, and not anyone's time building the search."""
    started, finished = parse_time(report.started), parse_time(report.finished)
    if started is None or finished is None or finished < started:
        return None
    return finished - started


def duration_text(td: timedelta) -> str:
    """'26 hours, 3 minutes, 5 seconds': hours are not rolled into days, and
    nothing is rounded, so the figure can be billed as it stands."""
    total = int(td.total_seconds())
    parts = [(total // 3600, "hour"), (total % 3600 // 60, "minute"), (total % 60, "second")]
    words = [f"{n:,} {unit}{'' if n == 1 else 's'}" for n, unit in parts if n]
    return ", ".join(words) or "0 seconds"


def _date(dt: datetime) -> str:
    return f"{dt:%B} {dt.day}, {dt:%Y}"


def _clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt:%M:%S %p}"


def _when(report: SearchReport) -> str:
    """When the export ran, in the container's local zone: 'March 4, 2021,
    7:02:03 AM to 8:03:04 AM CST' when both times are known, else the day it
    started, else the start time as written."""
    started, finished = parse_time(report.started), parse_time(report.finished)
    if started is None:
        return report.started
    s = started.astimezone()
    if run_time(report) is None:
        return _date(s)
    f = finished.astimezone()
    if _date(s) == _date(f) and s.tzname() == f.tzname():
        return f"{_date(s)}, {_clock(s)} to {_clock(f)} {f.tzname()}"
    return f"{_date(s)}, {_clock(s)} {s.tzname()} to {_date(f)}, {_clock(f)} {f.tzname()}"


def context(report: SearchReport | None) -> dict[str, Any] | None:
    """What the templates' "The search" section shows."""
    if report is None:
        return None
    elapsed = run_time(report)
    rows = [
        (label, value)
        for label, value in (
            ("Case", report.case_name),
            ("Search", report.search_name),
            ("Export", report.export_name),
            ("Exported", _when(report) if report.started else ""),
            ("Run time", duration_text(elapsed) if elapsed is not None else ""),
        )
        if value
    ]
    filtered = report.security_filter.strip().lower() == "yes"
    return {
        "from_report": report.source == "report",
        "rows": rows,
        "query": report.query,
        # Microsoft documents what these two mean; how the keyword lines
        # between (c:s) combine, it does not, so nothing more is said.
        "segments": "(c:s)" in report.query,
        "conditions": "(c:c)" in report.query,
        "filtered": filtered,
        "location_restriction": report.location_restriction if filtered else "",
        "content_restriction": report.content_restriction if filtered else "",
        "truncated": maybe_truncated(report),
        "run_time": elapsed is not None,
    }


def manifest_entry(report: SearchReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    started, finished = parse_time(report.started), parse_time(report.finished)
    elapsed = run_time(report)
    return {
        "source": report.source,
        "sha256": report.sha256,
        "query": report.query,
        "job_id": report.job_id or None,
        "case_name": report.case_name or None,
        "search_name": report.search_name or None,
        "export_name": report.export_name or None,
        "started_utc": started.isoformat() if started else (report.started or None),
        "finished_utc": finished.isoformat() if finished else (report.finished or None),
        "run_time_seconds": int(elapsed.total_seconds()) if elapsed is not None else None,
        "security_filter_applied": report.security_filter or None,
        "location_restriction": report.location_restriction or None,
        "content_restriction": report.content_restriction or None,
    }
