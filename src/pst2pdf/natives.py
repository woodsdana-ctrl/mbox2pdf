"""Original attachment files written beside the volumes.

``--keep-natives`` writes them next to a production run's volumes, named for
the message that first carried them. ``--triage`` makes them the point of the
run: every distinct attachment gets an ID (``A0042``) in order of first
reference, is written once as ``A0042 <name>``, and gets a row in an index
the reviewers fill in and pass back as ``--decisions``. Every page in the triage
volumes that stands where an attachment was names its ID.

Everything this module writes is kept back: the files are unredacted, and
the index carries subjects and senders.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from . import survey
from .model import MessageBundle, NativeFile

NATIVES_DIR_NAME = "Original attachments - DO NOT RELEASE"
INDEX_CSV_NAME = "Attachment index - DO NOT RELEASE.csv"
README_NAME = "README.txt"

#: Characters of the original name kept after the ID. A synced cloud folder's
#: path can already use about half of Windows' 260.
FILE_NAME_LIMIT = 60

#: convert.detect's kind -> the word the reviewer sees in the Type column
#: (the survey's labels, so the two CSVs agree).
KIND_LABELS = {
    "office": survey.TYPE_LABELS["libreoffice"],
    "image": survey.TYPE_LABELS["image"],
    "pdf": survey.TYPE_LABELS["pdf"],
    "text": survey.TYPE_LABELS["text"],
    "html": survey.TYPE_LABELS["html"],
    "eml": survey.TYPE_LABELS["message"],
    "msg": survey.TYPE_LABELS["message"],
    "ics": survey.TYPE_LABELS["calendar"],
    "zip": survey.TYPE_LABELS["container"],
    "": "Not recognized",
}

INDEX_COLUMNS = (
    "id",
    "file",
    "filename",
    "type",
    "size_bytes",
    "copies",
    "messages",
    "first_message",
    "first_message_date",
    "first_message_from",
    "first_message_subject",
    "last_message_date",
    "sha256",
    "status",
    # The reviewers fill these in and pass the file back with --decisions.
    "decision",
    "note",
    "outcome",
)

_UNSAFE_RE = re.compile(r'[\x00-\x1f<>:"|?*]')


def safe_filename(name: str, default: str = "attachment", limit: int = 120) -> str:
    """*name* as a file name that works on Windows: the part after the last
    slash, without characters Windows refuses or trailing dots and spaces,
    and at most *limit* characters with a short extension kept."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_RE.sub("_", name).strip(". ")
    if not name:
        return default
    if len(name) > limit:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: limit - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            name = name[:limit].rstrip(". ")
    return name


def file_name(file_id: str, filename: str) -> str:
    """'A0042 Budget FY27.xlsx'. The ID keeps names unique and Explorer's
    sort in message order, and defuses Windows' reserved names (CON, NUL),
    which are reserved only as a whole stem."""
    return f"{file_id} {safe_filename(filename, limit=FILE_NAME_LIMIT)}"


def _kind(filename: str, content_type: str, content: bytes) -> str:
    from . import convert  # detection only: nothing is converted

    kind, _ext = convert.detect(filename, content_type, content)
    return kind or ""


def assign_ids(bundles: list[MessageBundle]) -> list[NativeFile]:
    """One NativeFile per distinct sha256 among the slots that stand for an
    attachment (``native``, or ``withheld`` by a decision), numbered in
    message order. Expanded zips are containers and get no ID; their files
    do. Sets ``file_id`` on every slot. Withheld and omitted bytes get an ID
    but no file, so a triage run with ``--decisions`` numbers every file as
    the run without it did."""
    slots = []
    for bundle in sorted(bundles, key=lambda b: b.seq):
        for entry in bundle.attachments:
            status = entry.converted.status if entry.converted is not None else ""
            if status in ("native", "withheld"):
                slots.append((bundle, entry))
    distinct = len({entry.sha256 for _, entry in slots})
    width = max(4, len(str(distinct)))
    files: list[NativeFile] = []
    by_sha: dict[str, NativeFile] = {}
    for bundle, entry in slots:
        nf = by_sha.get(entry.sha256)
        if nf is None:
            conv = entry.converted
            att = bundle.model.attachments[entry.index - 1]
            if conv.status == "native":
                status = "file"
            else:
                status = "omitted" if conv.decision == "omit" else "withheld"
            nf = NativeFile(
                id=f"A{len(files) + 1:0{width}d}",
                filename=entry.filename,
                size=entry.size,
                content_type=entry.content_type,
                sha256=entry.sha256,
                kind=_kind(entry.filename, entry.content_type, att.content),
                status=status,
            )
            if status == "file":
                nf.file_name = file_name(nf.id, entry.filename)
            files.append(nf)
            by_sha[entry.sha256] = nf
        nf.carriers.append((bundle.seq, entry.index))
        entry.file_id = nf.id
    return files


def write_files(files: list[NativeFile], bundles: list[MessageBundle], output_dir: Path) -> Path:
    """Each file with a ``file_name``, once, exactly as attached, plus a README."""
    root = Path(output_dir) / NATIVES_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    (root / README_NAME).write_text(
        "These are the original attachment files, exactly as they were sent.\r\n"
        "They have NOT been redacted and must not be released.\r\n"
        "\r\n"
        "Each file's name starts with its ID (such as A0042). The page that stands\r\n"
        "where the attachment was in the triage volumes gives the same ID, and so\r\n"
        f"does its row in \"{INDEX_CSV_NAME}\".\r\n"
        "\r\n"
        "To decide what is produced, fill in the decision column of that index and\r\n"
        "send it back. Deleting or changing a file in this folder changes nothing.\r\n",
        encoding="utf-8",
    )
    by_seq = {b.seq: b for b in bundles}
    for nf in files:
        if nf.file_name is None:
            continue
        seq, index = nf.carriers[0]
        (root / nf.file_name).write_bytes(by_seq[seq].model.attachments[index - 1].content)
    return root


def write_kept(bundles: list[MessageBundle], output_dir: Path) -> Path:
    """``--keep-natives``: each attachment the volumes show, once, named for
    the message that first carried it ('0012-01 name.ext'). An attachment in
    the appendix volumes is written once however many messages carried it.
    Withheld copies are not written: the decision was not to produce them."""
    root = Path(output_dir) / NATIVES_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    (root / README_NAME).write_text(
        "These are the original attachment files, exactly as they were sent.\r\n"
        "They have NOT been redacted and must not be released.\r\n"
        "\r\n"
        "Each name starts with the message that carried the file and its place\r\n"
        "among that message's attachments (0012-01 is message 12's first).\r\n"
        "Where an attachment could be converted, its pages are in the volumes.\r\n",
        encoding="utf-8",
    )
    appendix_written: set[int] = set()
    for bundle in bundles:
        for entry in bundle.attachments:
            if entry.duplicate_of is not None or entry.appendix_item in appendix_written:
                continue
            if entry.converted is not None and entry.converted.status == "withheld":
                continue
            if entry.appendix_item is not None:  # identical copies share one item: write it once
                appendix_written.add(entry.appendix_item)
            att = bundle.model.attachments[entry.index - 1]
            (root / f"{bundle.seq:04d}-{entry.index:02d} {safe_filename(entry.filename)}").write_bytes(att.content)
    return root


def build_index_csv(files: list[NativeFile], bundles: list[MessageBundle], rules=None, outcome=None) -> str:
    """One row per file, in ID order, with empty decision and note columns
    for the reviewers. The columns ``decisions.load`` reads (filename, sha256,
    decision, note) are the survey CSV's, so the file goes back as
    ``--decisions`` unchanged. After a run with ``--decisions`` the rows carry
    the decisions forward, as the survey CSV does: a sha256 row keeps its
    decision, rows added by hand (a filename or a pattern) follow at the end,
    and the outcome column says what each decision did."""
    rules = list(rules or [])
    by_sha = {r.sha256: r for r in rules if r.sha256}
    results = outcome.results() if outcome is not None else {}
    by_seq = {b.seq: b for b in bundles}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(INDEX_COLUMNS)
    for nf in files:
        seqs = sorted({seq for seq, _ in nf.carriers})
        first = by_seq[seqs[0]].model
        dates = [by_seq[s].model.date for s in seqs if by_seq[s].model.date is not None]
        rule = by_sha.get(nf.sha256)
        row = {
            "id": nf.id,
            "file": nf.file_name or "",
            "filename": nf.filename,
            "type": KIND_LABELS.get(nf.kind, KIND_LABELS[""]),
            "size_bytes": nf.size,
            "copies": len(nf.carriers),
            "messages": "; ".join(str(s) for s in seqs),
            "first_message": seqs[0],
            "first_message_date": first.date.isoformat() if first.date else "",
            "first_message_from": " ".join(first.from_addr.split()),
            "first_message_subject": " ".join((first.subject or "").split()),
            "last_message_date": max(dates).isoformat() if dates else "",
            "sha256": nf.sha256,
            "status": nf.status,
            "decision": rule.decision if rule else "",
            "note": rule.note if rule else "",
            "outcome": results.get(nf.sha256, ""),
        }
        writer.writerow([survey.excel_cell(row[c]) for c in INDEX_COLUMNS])
    for rule in rules:
        if not rule.sha256:
            row = {"filename": rule.filename, "decision": rule.decision, "note": rule.note}
            writer.writerow([survey.excel_cell(row.get(c, "")) for c in INDEX_COLUMNS])
    return buf.getvalue()


def write_index_csv(files: list[NativeFile], bundles: list[MessageBundle], output_dir: Path, rules=None, outcome=None) -> Path:
    return survey.write_excel_csv(Path(output_dir) / INDEX_CSV_NAME, build_index_csv(files, bundles, rules, outcome))
