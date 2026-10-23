"""Read-only, one-message-at-a-time MBOX intake into the shared review pipeline.

The stdlib mailbox parser indexes envelope boundaries on disk. Decoded attachment
bytes are spooled before reading the next message. Models and body HTML remain
in memory for sorting/rendering; this is not a constant-memory pipeline.
"""
from __future__ import annotations

import csv
import hashlib
import json
import mailbox
import os
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from zoneinfo import ZoneInfo

from . import parse
from .model import DiskAttachment, MessageModel


class _ReadOnlyMbox(mailbox.mbox):
    """Use stdlib boundary indexing with an exclusively rb source handle.

    mailbox.mbox's initializer always tries rb+ even with create=False.
    Initialize its single-file reader state explicitly (Python 3.12+).
    Never expose mutation methods to the pipeline.
    """
    def __init__(self, path: Path):
        mailbox.Mailbox.__init__(self, str(path), factory=None, create=False)
        self._file = path.open("rb")
        self._toc = None
        self._next_key = 0
        self._pending = self._pending_sync = self._locked = False
        self._file_length = None

    def close(self):
        self._file.close()


def discover(source: Path) -> list[Path]:
    if source.is_file() and source.suffix.lower() == ".mbox":
        return [source]
    if source.is_dir():
        def unreadable(exc):
            raise exc
        try:
            paths = sorted(Path(base) / name for base, _, names in os.walk(source, onerror=unreadable)
                           for name in names if Path(name).suffix.lower() == ".mbox")
        except OSError as exc:
            raise ValueError(f"could not enumerate MBOX inputs: {exc}") from exc
        if paths:
            return paths
    raise ValueError(f"expected an .mbox file or directory containing .mbox files: {source}")


def fingerprint(msg) -> str:
    """Conservative identity: decoded relevant headers and complete MIME content.

    Labels, archive paths, MIME boundaries and transfer encoding are not identity.
    Content type, filename, CID, disposition, decoded bytes and MIME structure are.
    No URL or whitespace stripping. Missing Message-ID uses this same digest.
    """
    headers = ["From", "To", "Cc", "Bcc", "Subject", "Date", "Message-ID", "In-Reply-To", "References"]
    def mime(part):
        payload = part.get_payload()
        data = [part.get_content_type(), part.get_content_charset(), part.get_filename(),
                parse._header(part, "Content-ID"), part.get_content_disposition()]
        if isinstance(payload, list):
            data.append([mime(child) for child in payload])
        else:
            data.append(hashlib.sha256(part.get_payload(decode=True) or b"").hexdigest())
        return data
    data = [[(h, [str(v) for v in msg.get_all(h, [])]) for h in headers], mime(msg)]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def spool_attachments(models, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for model in models:
        for i, att in enumerate(model.attachments):
            if isinstance(att, DiskAttachment):
                continue
            digest = att.sha256
            path = directory / digest
            if not path.exists():
                with path.open("xb") as fh:
                    fh.write(att.content)
                path.chmod(0o600)
            model.attachments[i] = DiskAttachment(att.filename, path, att.content_type, att.size, digest)


def read(source: Path, work: Path, stats: dict, display_timezone: str | None = None) -> list[MessageModel]:
    paths = discover(source)
    zone = ZoneInfo(display_timezone) if display_timezone else None
    models = []
    stats["intake_archives"] = []
    stats["intake_errors"] = []
    stats["incomplete"] = False
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    for archive_no, path in enumerate(paths, 1):
        record = {"archive": str(path.resolve()), "messages": 0, "complete": True}
        stats["intake_archives"].append(record)
        try:
            before = path.stat()
            # Validate the envelope before the stdlib boundary scan.
            with path.open("rb") as fh:
                first = fh.readline()
                if first and not first.startswith(b"From "):
                    raise ValueError("missing initial MBOX envelope delimiter")
                fh.seek(0)
                box = _ReadOnlyMbox(path)
                try:
                    for position, key in enumerate(box.iterkeys(), 1):
                        raw_path = work / f"{archive_no:04d}-{position:09d}.eml"
                        association = {"archive": str(path.resolve()), "position": position, "labels": []}
                        try:
                            with box.get_file(key) as item:
                                raw = item.read()
                            # MBOXO escapes a body line beginning From with one >.
                            raw_path.write_bytes(raw)
                            chunks = re.split(rb"(\r?\n\r?\n)", raw, maxsplit=1)
                            if len(chunks) == 3:
                                raw = chunks[0] + chunks[1] + re.sub(rb"(?m)^>From ", b"From ", chunks[2])
                            raw_path.chmod(0o600)
                            msg = BytesParser(policy=policy.default).parsebytes(raw)
                            identity = fingerprint(msg)
                            model = parse.parse_message(msg, raw_path, readpst_compat=False)
                            model.identity_fingerprint = identity
                            model.gmail_labels = list(dict.fromkeys(
                                label.strip() for value in msg.get_all("X-Gmail-Labels", [])
                                for label in next(csv.reader([str(value)])) if label.strip()
                            ))
                            association["labels"] = model.gmail_labels.copy()
                            for part in msg.walk():
                                model.intake_issues.extend(type(d).__name__ for d in part.defects)
                                for _, header in part.items():
                                    model.intake_issues.extend(type(d).__name__ for d in getattr(header, "defects", ()))
                            if not model.date:
                                model.intake_issues.append("Missing or malformed Date header")
                            elif model.date.tzinfo is None:
                                model.intake_issues.append("Date has unknown timezone; original wall time retained")
                            if model.date and model.date.tzinfo:
                                model.date = model.date.astimezone(zone) if zone else model.date.astimezone()
                            if not model.from_addr:
                                model.intake_issues.append("Missing From header")
                        except Exception as exc:
                            reason = f"Message parsing failed ({type(exc).__name__}); inspect the retained source"
                            model = MessageModel(raw_path, "", "(message could not be parsed)", "", "", "", None,
                                                 "<p>Message could not be parsed. See the internal intake report and original archive.</p>")
                            model.identity_fingerprint = hashlib.sha256((str(path.resolve()) + str(position)).encode()).hexdigest()
                            model.intake_issues.append(reason)
                            stats["errors"] += 1
                        model.source_format = "mbox"
                        model.source_associations = [association]
                        spool_attachments([model], work / "attachments")
                        models.append(model)
                        record["messages"] += 1
                finally:
                    box.close()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("source changed while reading")
        except Exception as exc:
            record["complete"] = False
            stats["incomplete"] = True
            stats["errors"] += 1
            stats["intake_errors"].append({"archive": str(path.resolve()), "reason": f"{type(exc).__name__}: {exc}"})
    return models
