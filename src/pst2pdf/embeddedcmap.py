"""Teach pdfminer to read Type0 fonts whose Encoding is an embedded CMap.

pdfminer (20260107) looks an Encoding up by its CMapName only, and has no
parser for encoding CMaps at all: its CMapParser fills ToUnicode maps, and
its named CMaps ship prebuilt. An embedded CMap stream has a name no
database knows, so pdfminer falls back to an empty CMap and every character
in that font decodes to nothing: the text is on the page, drawn and
searchable in Acrobat, but premark cannot see it.

WeasyPrint writes one (``/WP-Encod-0``) whenever a font lacks a glyph the
text needs, emoji and CJK in mail bodies most often. Each missing character
gets its own code past the font's last glyph, ToUnicode maps that code to the
real character, and the CMap sends it to CID 0 so it is drawn as ``.notdef``.
Every other code maps to itself. One ✅ in a subject was enough to hide the
rest of the page's regular-weight text from premark.

The PDF rules pdfminer is taught here: ToUnicode is keyed by character code,
and a glyph's advance comes from the W entry of the CID the code maps to. So
a font with an embedded CMap and a ToUnicode decodes to character codes
(``to_unichr`` then reads ToUnicode correctly) and looks its widths up by the
code's CID. Getting the widths right matters as much as the text: WeasyPrint's
W array can give a missing character's code a width different from CID 0's,
viewers advance by CID 0's, and a wrong advance shifts every later character
on the line, and the redaction box drawn over it. A font with an embedded
CMap but no ToUnicode decodes to CIDs, as pdfminer does for named CMaps.

Only what an encoding CMap needs to say is read: codespace ranges, cidchar,
cidrange, and WMode. A CMap that builds on another (``usecmap``), or a
vertical one, is left as pdfminer had it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from pdfminer.cmapdb import CMapBase
from pdfminer.pdffont import PDFCIDFont
from pdfminer.pdftypes import PDFStream, resolve1

_installed = False

_HEX = r"<([0-9A-Fa-f\s]*)>"
_SECTION = re.compile(r"begin(codespacerange|cidchar|cidrange)(.*?)end\1", re.S)
_PAIR = re.compile(_HEX + r"\s*" + _HEX)  # codespacerange: <lo> <hi>
_CHAR = re.compile(_HEX + r"\s*(\d+)")  # cidchar: <code> cid
_RANGE = re.compile(_HEX + r"\s*" + _HEX + r"\s*(\d+)")  # cidrange: <lo> <hi> cid


def _hex(text: str) -> bytes:
    return bytes.fromhex("".join(text.split()))


@dataclass
class EncodingCMap:
    """An embedded encoding CMap: how to cut a string into codes, and each
    code's CID. A code in a codespace but in no mapping is CID 0 (.notdef)."""

    spaces: list[tuple[bytes, bytes]] = field(default_factory=list)
    chars: dict[bytes, int] = field(default_factory=dict)
    ranges: list[tuple[bytes, bytes, int]] = field(default_factory=list)
    vertical: bool = False
    extends: bool = False  # usecmap: builds on a CMap not read here

    @classmethod
    def parse(cls, data: bytes) -> EncodingCMap:
        text = data.decode("latin-1")
        cmap = cls(vertical=bool(re.search(r"/WMode\s+1\b", text)), extends="usecmap" in text)
        for kind, body in _SECTION.findall(text):
            if kind == "codespacerange":
                cmap.spaces += [(_hex(lo), _hex(hi)) for lo, hi in _PAIR.findall(body)]
            elif kind == "cidchar":
                cmap.chars.update((_hex(code), int(cid)) for code, cid in _CHAR.findall(body))
            else:
                cmap.ranges += [(_hex(lo), _hex(hi), int(cid)) for lo, hi, cid in _RANGE.findall(body)]
        return cmap

    def usable(self) -> bool:
        return bool(self.spaces) and bool(self.chars or self.ranges) and not (self.vertical or self.extends)

    def _in_space(self, code: bytes) -> bool:
        return any(
            len(lo) == len(code) and all(a <= b <= c for a, b, c in zip(lo, code, hi)) for lo, hi in self.spaces
        )

    def codes(self, data: bytes) -> Iterator[bytes]:
        """Cut *data* into codes by the codespace ranges (PDF 32000 9.7.6.2)."""
        longest = max(len(lo) for lo, _ in self.spaces)
        shortest = min(len(lo) for lo, _ in self.spaces)
        i = 0
        while i < len(data):
            for n in range(1, longest + 1):
                if self._in_space(data[i : i + n]):
                    break
            else:
                n = shortest  # no range matches: the spec's .notdef fallback
            yield data[i : i + n]
            i += n

    def cid(self, code: bytes) -> int:
        cid = self.chars.get(code)
        if cid is not None:
            return cid
        value = int.from_bytes(code, "big")
        for lo, hi, first in self.ranges:
            if len(lo) == len(code) and int.from_bytes(lo, "big") <= value <= int.from_bytes(hi, "big"):
                return first + value - int.from_bytes(lo, "big")
        return 0


class _Decoder(CMapBase):
    """What pdfminer calls the font's cmap: yields each character code as an
    integer (big-endian, as ToUnicode keys them), or its CID."""

    def __init__(self, cmap: EncodingCMap, by_code: bool) -> None:
        CMapBase.__init__(self)
        self.encoding = cmap
        self.by_code = by_code

    def decode(self, code: bytes) -> Iterator[int]:
        for c in self.encoding.codes(code):
            yield int.from_bytes(c, "big") if self.by_code else self.encoding.cid(c)


class _WidthsByCID:
    """The font's widths looked up by a code's CID. pdfminer only calls get()."""

    def __init__(self, widths: dict, cmap: EncodingCMap, size: int) -> None:
        self.widths = widths
        self.cmap = cmap
        self.size = size  # code length: the codes pdfminer passes back are integers

    def get(self, code, default=None):
        if not isinstance(code, int):
            return self.widths.get(code, default)
        cid = self.cmap.cid(code.to_bytes(self.size, "big"))
        return self.widths.get(cid, default)


def read_embedded_cmap(font: PDFCIDFont, spec) -> None:
    """Replace pdfminer's empty stand-in for an embedded Encoding CMap."""
    encoding = resolve1(spec.get("Encoding"))
    if not isinstance(encoding, PDFStream):
        return
    cmap = EncodingCMap.parse(encoding.get_data())
    if not cmap.usable():
        return
    by_code = "ToUnicode" in spec
    font.cmap = _Decoder(cmap, by_code)
    sizes = {len(lo) for lo, _ in cmap.spaces}
    if by_code and len(sizes) == 1:
        font.widths = _WidthsByCID(font.widths, cmap, sizes.pop())
    elif by_code:
        # Mixed code lengths: an integer no longer says which bytes it came
        # from, so decode to CIDs and read ToUnicode as pdfminer always has.
        font.cmap = _Decoder(cmap, by_code=False)


def install() -> None:
    """Wrap PDFCIDFont.__init__ once per process."""
    global _installed
    if _installed:
        return
    original = PDFCIDFont.__init__

    def __init__(self, rsrcmgr, spec, *args, **kwargs):
        original(self, rsrcmgr, spec, *args, **kwargs)
        read_embedded_cmap(self, spec)

    PDFCIDFont.__init__ = __init__
    _installed = True
