"""Parse a readpst-emitted .ics calendar item into a MessageModel.

This module shadows the stdlib ``calendar`` only for *relative* imports
(``from . import calendar``); absolute ``import calendar`` elsewhere in the
package still resolves to the stdlib because the package uses absolute
imports throughout. Do not add ``import calendar`` inside this package
expecting the stdlib one, and do not run modules from ``src/pst2pdf`` with
that directory on ``sys.path``.
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import date as date_type, datetime, time, timedelta
from html import escape
from pathlib import Path

from icalendar import Calendar

from .model import Attachment, MessageModel, Participant
from .parse import _inner_html, _IMG_RE, clean_html, content_hash, normalize_message_id

log = logging.getLogger(__name__)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _cal_address(value) -> tuple[str, str]:
    """(display name, lowercased address) from an ORGANIZER/ATTENDEE value."""
    params = getattr(value, "params", {}) or {}
    name = _text(params.get("CN")).strip().strip('"').strip()
    addr = _text(value).strip()
    if addr.lower().startswith("mailto:"):
        addr = addr[len("mailto:") :]
    addr = addr.strip().lower()
    if name.lower() == addr:
        name = ""
    return name, addr


def _display(name: str, addr: str) -> str:
    if name and addr:
        return f"{name} <{addr}>"
    return name or addr


def _to_datetime(value) -> datetime | None:
    dt = getattr(value, "dt", value)
    if isinstance(dt, datetime):
        return dt
    if isinstance(dt, date_type):
        return datetime.combine(dt, time.min)
    return None


def _local(dt: datetime | None) -> datetime | None:
    """Aware times in the container's local zone (TZ), so the body agrees with
    the header, which cli.parse_tree converts the same way. Naive stays naive."""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone()
    return dt


def _when_row(start: datetime | None, end: datetime | None, all_day: bool) -> tuple[str, str] | None:
    """The body row that adds to the header's "When" (the start time) without
    repeating it: the end time, or the day span of an all-day event."""
    if start is None:
        return None
    if all_day:
        if end and (end - start).days > 1:
            return "All day", f"{start:%Y-%m-%d} to {end - timedelta(days=1):%Y-%m-%d}"
        return "All day", f"{start:%Y-%m-%d}"
    if end is None:
        return None
    tz = f" {end:%Z}".rstrip() if end.tzinfo else ""
    if end.date() == start.date():
        return "Ends", f"{end:%H:%M}{tz}"
    return "Ends", f"{end:%Y-%m-%d %H:%M}{tz}"


def _attachments(event) -> list[Attachment]:
    out: list[Attachment] = []
    for n, item in enumerate(_as_list(event.get("ATTACH")), start=1):
        params = getattr(item, "params", {}) or {}
        if _text(params.get("VALUE")).upper() != "BINARY" and not hasattr(item, "obj"):
            continue  # URI attachment: nothing to convert, and never fetched
        try:
            raw = item.to_ical() if hasattr(item, "to_ical") else _text(item).encode("ascii")
            content = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            log.warning("calendar attachment %d could not be decoded; skipped", n)
            continue
        filename = (
            _text(params.get("X-FILENAME"))
            or _text(params.get("FILENAME"))
            or f"attachment-{n}"
        )
        out.append(
            Attachment(
                filename=filename,
                content=content,
                content_type=_text(params.get("FMTTYPE")) or "application/octet-stream",
            )
        )
    return out


def _description_html(event) -> str:
    """Prefer Outlook's X-ALT-DESC HTML, cleaned; else the escaped plain text."""
    for alt in _as_list(event.get("X-ALT-DESC")):
        params = getattr(alt, "params", {}) or {}
        if "html" in _text(params.get("FMTTYPE")).lower() or "<" in _text(alt):
            html = clean_html(_inner_html(_text(alt))).strip()
            if html:
                return html
    plain = _text(event.get("DESCRIPTION")).strip()
    if not plain:
        return ""
    return "<p>" + escape(plain).replace("\r\n", "\n").replace("\n", "<br>") + "</p>"


def parse_ics(path: Path, folder: str = "") -> MessageModel:
    """Parse the first VEVENT in an .ics file into a ``kind="calendar"`` model."""
    raw = path.read_bytes()
    events = []
    try:
        cal = Calendar.from_ical(raw)
        events = cal.walk("VEVENT")
    except Exception as exc:  # icalendar raises assorted ValueError subclasses
        log.warning("%s: unparseable calendar item (%s)", path.name, exc)

    if not events:
        model = MessageModel(
            source=path,
            folder=folder,
            subject="",
            from_addr="",
            to_addrs="",
            cc_addrs="",
            date=None,
            body_html="<p><em>(calendar item could not be read)</em></p>",
            kind="calendar",
        )
        model.content_hash = content_hash(None, "", "", "", model.body_html)
        return model

    event = events[0]
    if len(events) > 1:
        log.debug("%s: %d events, using the first", path.name, len(events))

    subject = _text(event.get("SUMMARY"))
    org_name, org_addr = _cal_address(event.get("ORGANIZER")) if event.get("ORGANIZER") else ("", "")
    from_addr = _display(org_name, org_addr)

    attendees: list[tuple[str, str]] = []
    for att in _as_list(event.get("ATTENDEE")):
        pair = _cal_address(att)
        if any(pair) and pair not in attendees:
            attendees.append(pair)
    to_addrs = ", ".join(_display(n, a) for n, a in attendees)

    participants: list[Participant] = []
    for name, addr in ([(org_name, org_addr)] if from_addr else []) + attendees:
        p = Participant(name=name, address=addr)
        if p not in participants:
            participants.append(p)

    dtstart = event.get("DTSTART")
    dtend = event.get("DTEND")
    all_day = dtstart is not None and not isinstance(getattr(dtstart, "dt", None), datetime)
    start = _local(_to_datetime(dtstart))
    end = _local(_to_datetime(dtend))
    location = _text(event.get("LOCATION")).strip()

    # Organizer, attendees and the start time are in the message header
    # (message.html); the body only carries what the header cannot show.
    rows = [_when_row(start, end, all_day), ("Where", location)]
    parts = ['<table class="calendar">']
    for row in rows:
        if row and row[1]:
            label, value = row
            parts.append(f"<tr><th>{label}</th><td>{escape(value)}</td></tr>")
    parts.append("</table>")
    description = _description_html(event)
    if description:
        parts.append(f'<div class="calendar-description">{description}</div>')
    body_html = "".join(parts)

    return MessageModel(
        source=path,
        folder=folder,
        subject=subject,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs="",
        date=start,
        body_html=body_html,
        attachments=_attachments(event),
        kind="calendar",
        message_id=normalize_message_id(_text(event.get("UID"))),
        participants=participants,
        has_images=bool(_IMG_RE.search(body_html)),
        content_hash=content_hash(start, from_addr, to_addrs, subject, body_html),
    )
