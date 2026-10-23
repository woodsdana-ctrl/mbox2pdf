import time
from datetime import datetime, timezone

import pytest

from pst2pdf import calendar
from pst2pdf.calendar import parse_ics
from pst2pdf.model import Participant

from conftest import PNG_BYTES, make_ics


@pytest.fixture(autouse=True)
def chicago(monkeypatch):
    """Aware times are shown in the local zone, so pin it for the assertions."""
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.delenv("TZ")
    time.tzset()


def test_relative_import_resolves_to_package_module():
    # The package module shadows stdlib ``calendar`` only via relative import.
    import calendar as stdlib_calendar

    assert hasattr(calendar, "parse_ics")
    assert hasattr(stdlib_calendar, "monthrange")
    assert calendar is not stdlib_calendar


def test_basic_event(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(make_ics())
    model = parse_ics(path, folder="Calendar")

    assert model.kind == "calendar"
    assert model.folder == "Calendar"
    assert model.source == path
    assert model.subject == "Budget meeting"
    assert model.from_addr == "Alice Example <alice@example.com>"
    assert model.to_addrs == "Bob Example <bob@example.com>, Carol Q. Example <carol@example.com>"
    assert model.cc_addrs == ""
    assert model.date == datetime.fromisoformat("2024-03-15T09:12:00-05:00")
    assert model.message_id == "evt-1@example.com"
    assert model.attachments == []
    assert model.has_images is False
    assert model.content_hash


def test_body_block(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(make_ics())
    body = parse_ics(path).body_html

    # The header (message.html) shows the start, organizer and attendees, so
    # the body carries only what the header cannot: the end time and the place.
    assert "<th>Ends</th><td>10:12 CDT</td>" in body
    assert "<th>Where</th><td>Room 4</td>" in body
    assert "<th>When</th>" not in body
    assert "Organizer" not in body and "Attendees" not in body
    # Description newlines survive as <br>; the text is escaped.
    assert "Bring the numbers<br>Second line" in body


def test_participants_include_organizer_and_attendees(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(make_ics())
    assert parse_ics(path).participants == [
        Participant(name="Alice Example", address="alice@example.com"),
        Participant(name="Bob Example", address="bob@example.com"),
        Participant(name="Carol Q. Example", address="carol@example.com"),
    ]


def test_binary_attachment_decoded(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(
        make_ics(
            binary_attachments=[
                ("agenda.pdf", b"%PDF-1.4 hello", "application/pdf"),
                ("chart.png", PNG_BYTES, "image/png"),
            ],
            uri_attachments=["http://example.com/remote.doc"],
        )
    )
    model = parse_ics(path)

    assert [a.filename for a in model.attachments] == ["agenda.pdf", "chart.png"]
    assert model.attachments[0].content == b"%PDF-1.4 hello"
    assert model.attachments[0].content_type == "application/pdf"
    assert model.attachments[1].content == PNG_BYTES
    # URI attachments are never fetched and never listed.
    assert all("remote.doc" not in a.filename for a in model.attachments)


def test_html_alt_description_is_cleaned(tmp_path):
    path = tmp_path / "1.ics"
    html = (
        '<html><body><p>Bring <b>numbers</b> from '
        '<a href="https://example.com/budget">the budget</a></p>'
        '<img src="cid:x" alt="chart"></body></html>'
    )
    path.write_bytes(make_ics(x_alt_desc=html))
    model = parse_ics(path)

    assert "Bring <b>numbers</b>" in model.body_html
    assert "href" not in model.body_html
    assert "the budget [https://example.com/budget]" in model.body_html
    assert "alt=" not in model.body_html
    # A cid: image with no embedded data renders as nothing, so it needs no
    # manual area redaction and does not count as an image.
    assert model.has_images is False
    # The plain DESCRIPTION is not duplicated when the HTML version is used.
    assert "Second line" not in model.body_html


def test_all_day_event(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(
        make_ics(dtstart="DTSTART;VALUE=DATE:20240315", dtend="DTEND;VALUE=DATE:20240316")
    )
    model = parse_ics(path)

    assert model.date == datetime(2024, 3, 15, 0, 0)
    assert "<tr><th>All day</th><td>2024-03-15</td></tr>" in model.body_html


def test_multi_day_all_day_event(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(
        make_ics(dtstart="DTSTART;VALUE=DATE:20240315", dtend="DTEND;VALUE=DATE:20240318")
    )
    assert "<tr><th>All day</th><td>2024-03-15 to 2024-03-17</td></tr>" in parse_ics(path).body_html


def test_times_are_shown_in_the_local_zone(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(make_ics(dtstart="DTSTART:20240315T141200Z", dtend="DTEND:20240316T151200Z"))
    model = parse_ics(path)
    assert model.date == datetime(2024, 3, 15, 14, 12, tzinfo=timezone.utc)
    assert model.date.strftime("%H:%M %Z") == "09:12 CDT"
    assert "<th>Ends</th><td>2024-03-16 10:12 CDT</td>" in model.body_html


def test_event_without_end_has_no_when_row(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(make_ics(dtend=None))
    body = parse_ics(path).body_html
    assert "Ends" not in body and "When" not in body
    assert "<th>Where</th>" in body


def test_missing_optional_fields(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(
        make_ics(
            summary=None,
            uid=None,
            dtstart=None,
            dtend=None,
            organizer=None,
            attendees=(),
            location=None,
            description=None,
        )
    )
    model = parse_ics(path)

    assert model.subject == ""
    assert model.from_addr == ""
    assert model.to_addrs == ""
    assert model.date is None
    assert model.message_id is None
    assert model.participants == []
    assert model.body_html == '<table class="calendar"></table>'
    assert model.content_hash


def test_organizer_without_cn(tmp_path):
    path = tmp_path / "1.ics"
    path.write_bytes(make_ics(organizer="ORGANIZER:mailto:ALICE@example.com"))
    model = parse_ics(path)
    assert model.from_addr == "alice@example.com"
    assert model.participants[0] == Participant(name="", address="alice@example.com")


def test_unparseable_file_yields_placeholder(tmp_path):
    path = tmp_path / "broken.ics"
    path.write_bytes(b"this is not a calendar\n")
    model = parse_ics(path, folder="Calendar")

    assert model.kind == "calendar"
    assert model.date is None
    assert "could not be read" in model.body_html
    assert model.content_hash


def test_no_vevent_yields_placeholder(tmp_path):
    path = tmp_path / "empty.ics"
    path.write_bytes(b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")
    assert "could not be read" in parse_ics(path).body_html
