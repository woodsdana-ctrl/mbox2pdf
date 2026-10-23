"""The redaction word list: every distinct display name and email address from
the message headers, one per line, for Acrobat's Search & Redact import.

Names come first, then addresses, each block sorted case-insensitively.
Distinctness is case-insensitive too, since Acrobat's search ignores case
by default and a shorter list is easier for the reviewer to review. A display
name that is itself an address is listed once, among the addresses. Names
shorter than three characters are dropped: they are initials or junk and
would match inside ordinary words. A name with a parenthetical suffix, such
as a department, is listed with and without it, and a ``Last, First`` name
is also listed as ``First Last``, because bodies write people both ways.

The file is UTF-8 with CRLF line endings so it imports cleanly on Windows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from .model import MessageModel

FILENAME = "Redaction word list.txt"
MIN_NAME_LENGTH = 3

_PAREN_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _clean(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").split()).strip().strip('"').strip("'").strip()


def name_forms(name: str) -> list[str]:
    """The display name plus the forms Acrobat's search also needs to find it.

    ``Jane Doe (Finance)`` also yields ``Jane Doe``, and ``Doe, Jane`` also
    yields ``Jane Doe``, because the same person is written both ways inside
    message bodies. Order: the original first, then the derived forms.
    """
    forms = [name]
    base = _PAREN_SUFFIX_RE.sub("", name).strip()
    if base and base != name:
        forms.append(base)
    if base.count(",") == 1:
        last, _, first = base.partition(",")
        last, first = last.strip(), first.strip()
        if last and first and not first.endswith(":"):
            forms.append(f"{first} {last}")
    return forms


def _distinct_sorted(values: Iterable[str]) -> list[str]:
    seen: dict[str, str] = {}
    for v in values:
        key = v.casefold()
        if key not in seen:
            seen[key] = v
    return sorted(seen.values(), key=lambda s: (s.casefold(), s))


def collect(models: Iterable[MessageModel]) -> tuple[list[str], list[str]]:
    """Return (names, addresses), each distinct and sorted, from every model's participants."""
    names: list[str] = []
    addresses: list[str] = []
    for model in models:
        for p in model.participants:
            addr = _clean(p.address).lower()
            name = _clean(p.name)
            if addr and "@" in addr:
                addresses.append(addr)
            if name:
                if "@" in name:
                    # A display name that is really an address belongs with the addresses.
                    addresses.append(name.lower())
                else:
                    names.extend(f for f in name_forms(name) if len(f) >= MIN_NAME_LENGTH)
    address_set = {a.casefold() for a in addresses}
    names = [n for n in names if n.casefold() not in address_set]
    return _distinct_sorted(names), _distinct_sorted(addresses)


def write(models: Iterable[MessageModel], output_dir: Path) -> Path:
    """Write the word list into *output_dir* and return its path."""
    names, addresses = collect(models)
    lines = names + addresses
    out = Path(output_dir) / FILENAME
    out.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(line + "\r\n" for line in lines)
    out.write_bytes(data.encode("utf-8"))
    return out
