"""Expand zip attachments one level so their files become attachments too.

A zip on an email is usually a bundle of records, and every record inside it
is something the reviewer has to see and redact. Expansion is bounded so a
hostile archive cannot exhaust disk or memory: entry count, total size, and
compression ratio are all capped, nested zips are left as placeholders, and
encrypted entries are skipped and reported on the zip's own page.
"""

from __future__ import annotations

import io
import logging
import posixpath
import zipfile
from dataclasses import dataclass, field

from .model import Attachment

log = logging.getLogger(__name__)

MAX_ENTRIES = 200
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_RATIO = 100  # uncompressed / compressed above this (and above 10 MB) is a zip bomb
RATIO_FLOOR = 10 * 1024 * 1024

ZIP_EXTENSIONS = {"zip"}
ZIP_TYPES = {"application/zip", "application/x-zip-compressed"}


@dataclass
class Expansion:
    entries: list[Attachment] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "name: reason"


OFFICE_MARKERS = ("[Content_Types].xml", "mimetype")  # OOXML and OpenDocument are zips too


def looks_like_zip(att: Attachment) -> bool:
    """A real zip archive, by name or declared type. Zip magic bytes alone do
    not count: every .docx, .xlsx, .pptx and .odt is a zip inside."""
    ext = att.filename.rsplit(".", 1)[-1].lower() if "." in att.filename else ""
    if ext and ext not in ZIP_EXTENSIONS:
        return False
    return ext in ZIP_EXTENSIONS or att.content_type in ZIP_TYPES


def _clean_name(name: str) -> str | None:
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or None


def expand(att: Attachment, max_entry_bytes: int) -> Expansion | None:
    """Return the zip's files as attachments named ``<zip>/<entry>``, or None
    when *att* is not a readable zip. Never raises on hostile input."""
    if not looks_like_zip(att):
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(att.content))
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        log.info("%s: not a readable zip (%s)", att.filename, exc)
        return None
    result = Expansion()
    with zf:
        names = set(zf.namelist())
        if any(m in names for m in OFFICE_MARKERS):
            return None  # an Office document wearing a .zip name; convert it as a document
        infos = [i for i in zf.infolist() if not i.is_dir()]
        total = sum(i.file_size for i in infos)
        compressed = sum(i.compress_size for i in infos) or 1
        if len(infos) > MAX_ENTRIES:
            result.skipped.append(f"{len(infos)} files inside, more than the {MAX_ENTRIES} allowed; none expanded")
            return result
        if total > MAX_TOTAL_BYTES:
            result.skipped.append(f"{total // (1024 * 1024)} MB inside, more than allowed; none expanded")
            return result
        if total > RATIO_FLOOR and total / compressed > MAX_RATIO:
            result.skipped.append("compression ratio too high to be a normal archive; none expanded")
            return result
        for info in infos:
            name = _clean_name(info.filename)
            if name is None:
                continue
            label = f"{att.filename}/{name}"
            if info.flag_bits & 0x1:
                result.skipped.append(f"{name}: password protected")
                continue
            if info.file_size > max_entry_bytes:
                result.skipped.append(f"{name}: larger than the conversion limit")
                continue
            try:
                data = zf.read(info)
            except (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError) as exc:
                result.skipped.append(f"{name}: could not be read ({exc})")
                continue
            if len(data) != info.file_size:
                result.skipped.append(f"{name}: size mismatch, not trusted")
                continue
            ext = posixpath.splitext(name)[1].lower()
            content_type = "application/zip" if ext == ".zip" else "application/octet-stream"
            result.entries.append(Attachment(filename=label, content=data, content_type=content_type))
    return result


def expand_all(attachments: list[Attachment], max_entry_bytes: int) -> tuple[list[Attachment], dict[str, list[str]]]:
    """Replace each zip in *attachments* with itself followed by its files.

    Nested zips (a zip inside a zip) are not expanded again. Returns the new
    list and, per zip filename, the notes about what was skipped.
    """
    out: list[Attachment] = []
    notes: dict[str, list[str]] = {}
    for att in attachments:
        out.append(att)
        if "/" in att.filename:  # already an entry from an outer zip
            continue
        exp = expand(att, max_entry_bytes)
        if exp is None:
            continue
        out.extend(exp.entries)
        if exp.skipped:
            notes[att.filename] = exp.skipped
    return out, notes
