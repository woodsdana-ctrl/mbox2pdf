"""Shared fixture helpers: build .eml files with the stdlib email package."""

import struct
import zlib
from email.message import EmailMessage


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


# 1x1 transparent RGBA PNG, assembled chunk by chunk so it is guaranteed valid.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00\x00\x00\x00"))
    + _png_chunk(b"IEND", b"")
)


def make_message(
    subject="Quarterly report",
    html="<html><head><title>x</title></head><body><p>Hello <b>world</b></p></body></html>",
    plain="Hello world",
    attachments=(),
    inline_png_cid=None,
    date="Fri, 15 Mar 2024 09:12:03 -0500",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Alice Example <alice@example.com>"
    msg["To"] = "Bob Example <bob@example.com>"
    msg["Subject"] = subject
    if date:
        msg["Date"] = date
    if plain is not None:
        msg.set_content(plain)
    if html is not None:
        msg.add_alternative(html, subtype="html")
        if inline_png_cid:
            html_part = msg.get_body(preferencelist=("html",))
            html_part.add_related(
                PNG_BYTES, maintype="image", subtype="png", cid=f"<{inline_png_cid}>"
            )
    for filename, content in attachments:
        msg.add_attachment(
            content,
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )
    return msg


def write_eml(msg: EmailMessage, path) -> None:
    path.write_bytes(msg.as_bytes())


# ---------------------------------------------------------------------------
# v0.2 helpers (appended; the helpers above keep their signatures)
# ---------------------------------------------------------------------------

import base64  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from pst2pdf.model import MessageModel, Participant  # noqa: E402


def add_headers(msg: EmailMessage, **headers) -> EmailMessage:
    """Set or replace headers on a message built by make_message.

    Underscores in keyword names become hyphens (``message_id`` ->
    ``Message-ID``); the exact header case does not matter to the parser.
    """
    for name, value in headers.items():
        header = name.replace("_", "-")
        if header in msg:
            del msg[header]
        if value is not None:
            msg[header] = value
    return msg


def make_ics(
    summary="Budget meeting",
    uid="evt-1@example.com",
    dtstart="DTSTART;TZID=America/Chicago:20240315T091200",
    dtend="DTEND;TZID=America/Chicago:20240315T101200",
    organizer='ORGANIZER;CN="Alice Example":mailto:Alice@Example.com',
    attendees=(
        "ATTENDEE;CN=Bob Example;ROLE=REQ-PARTICIPANT:mailto:bob@example.com",
        'ATTENDEE;CN="Carol Q. Example":mailto:carol@example.com',
    ),
    location="Room 4",
    description="Bring the numbers\\nSecond line",
    x_alt_desc=None,
    binary_attachments=(),
    uri_attachments=(),
) -> bytes:
    """Build a minimal Outlook-style VEVENT as readpst would emit it."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", "BEGIN:VEVENT"]
    if uid is not None:
        lines.append(f"UID:{uid}")
    if summary is not None:
        lines.append(f"SUMMARY:{summary}")
    for prop in (dtstart, dtend, organizer):
        if prop:
            lines.append(prop)
    lines.extend(attendees)
    if location is not None:
        lines.append(f"LOCATION:{location}")
    if description is not None:
        lines.append(f"DESCRIPTION:{description}")
    if x_alt_desc is not None:
        lines.append(f"X-ALT-DESC;FMTTYPE=text/html:{x_alt_desc}")
    for filename, content, fmttype in binary_attachments:
        b64 = base64.b64encode(content).decode("ascii")
        lines.append(
            f"ATTACH;FMTTYPE={fmttype};X-FILENAME={filename};"
            f"ENCODING=BASE64;VALUE=BINARY:{b64}"
        )
    for uri in uri_attachments:
        lines.append(f"ATTACH:{uri}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines).encode("utf-8")


def make_model(
    source="Inbox/1.eml",
    folder=None,
    subject="Quarterly report",
    from_addr="Alice Example <alice@example.com>",
    to_addrs="Bob Example <bob@example.com>",
    date="2024-03-15T09:00:00+00:00",
    message_id=None,
    in_reply_to=None,
    references=(),
    thread_index=None,
    participants=None,
    content_hash=None,
    body_html="<p>body</p>",
    **extra,
) -> MessageModel:
    """Build a MessageModel directly, for organize() tests.

    ``date`` accepts an ISO string, a datetime, or None. ``participants``
    defaults to alice and bob; pass a list of addresses or Participants.
    """
    if isinstance(date, str):
        date = datetime.fromisoformat(date)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    source = Path(source)
    if folder is None:
        folder = str(source.parent) if str(source.parent) != "." else ""
    if participants is None:
        participants = ["alice@example.com", "bob@example.com"]
    parts = [
        p if isinstance(p, Participant) else Participant(name="", address=p)
        for p in participants
    ]
    if content_hash is None:
        content_hash = "hash-" + str(source).replace("/", "-")
    return MessageModel(
        source=source,
        folder=folder,
        subject=subject,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs="",
        date=date,
        body_html=body_html,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=list(references),
        thread_index=thread_index,
        participants=parts,
        content_hash=content_hash,
        **extra,
    )
