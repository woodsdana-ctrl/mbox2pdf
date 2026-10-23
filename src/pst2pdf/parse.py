"""Stage 2: parse a .eml file into a MessageModel.

Also home of ``clean_html``, the body-hygiene pass that strips every channel
through which unredacted data could leak out of the visible page (link
targets, alt text, scripts, hidden frames). ``convert.py`` imports it for
standalone HTML attachments, so keep it dependency-free and forgiving of the
broken markup real mail carries.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from . import images
from .model import Attachment, MessageModel, Participant  # noqa: F401

log = logging.getLogger(__name__)

# readpst emits the original compressed-RTF body under this name when it
# cannot convert it; it is a body representation, not a real attachment.
RTF_BODY_NAME = "rtf-body.rtf"

_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)
_STRIP_RE = re.compile(
    r"<!DOCTYPE[^>]*>|</?html[^>]*>|<head[^>]*>.*?</head>",
    re.IGNORECASE | re.DOTALL,
)
# Only embedded (data:) images count: a blocked remote image renders as nothing,
# so it needs no manual area redaction.
_IMG_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']?data:", re.IGNORECASE)
_MSGID_RE = re.compile(r"<([^<>]+)>")

# readpst writes a meeting message's own appointment properties as an
# attachment named ``i<rand()>.ics`` with ``DTSTAMP:<extraction time>``, so
# name and bytes change on every extraction of the same archive, and a
# decision made on one run's sha256 matched nothing in the next.
READPST_ICS_NAME = "Calendar item.ics"
_READPST_ICS_NAME_RE = re.compile(r"i\d+\.ics")
_READPST_ICS_PRODID_RE = re.compile(rb"^PRODID:LibPST v", re.MULTILINE)
_DTSTAMP_RE = re.compile(rb"^DTSTAMP:[^\r\n]*(\r?\n)", re.MULTILINE)
_CREATED_RE = re.compile(rb"^CREATED:(\d{8}T\d{6}Z)\r?$", re.MULTILINE)


# ---------------------------------------------------------------------------
# HTML hygiene
# ---------------------------------------------------------------------------

# Removed together with everything inside them.
_DROP_WITH_CONTENT = frozenset(
    {"script", "style", "iframe", "object", "head", "title", "noscript"}
)
# Removed as tags only; they carry no visible content of their own. ``form``
# is here rather than above because mail bodies (newsletters especially) wrap
# visible text in a form, and the reviewer needs to see that text.
_DROP_TAG_ONLY = frozenset({"link", "meta", "input", "form", "base"})
# ``id`` and ``name`` go because WeasyPrint turns them into named
# destinations, a channel the redaction never sees. ``nowrap`` (on table
# cells) keeps a cell on one line however far past the page it runs, and
# ``class`` can only ever match pst2pdf's own template rules (the body's
# ``<style>`` blocks are gone), some of which forbid wrapping.
_DROP_ATTRS = frozenset({"alt", "title", "href", "id", "name", "nowrap", "class"})
# Inline style declarations that can park text off the page or make it
# invisible while it still reaches the PDF text layer (``overflow: hidden``
# clips text out of view but leaves it in the text layer), plus the properties
# WeasyPrint turns into bookmarks or running strings. ``display:none`` and
# ``visibility:hidden`` are left alone: WeasyPrint drops such elements
# entirely. Same-colour text (white on white) is not detected here; Acrobat's
# Sanitize step (Remove Hidden Information) is what covers it.
_DROP_STYLE_PROPS = frozenset(
    {
        "position", "left", "right", "top", "bottom", "opacity", "text-indent",
        "clip", "clip-path", "transform", "overflow", "overflow-x", "overflow-y",
        "bookmark-level", "bookmark-label", "bookmark-state", "string-set",
    }
)
_DROP_STYLE_PREFIX = "-weasy-"
# Declarations that stop body content from wrapping to the page width. Text
# pushed past the right margin is clipped from view but stays in the text
# layer, so the reviewer never sees it and Search & Redact still finds it.
# ``min-width`` and the wrapping properties are dropped so the template's
# ``overflow-wrap: break-word`` applies; ``white-space`` is kept but made to
# wrap (a line break the sender typed in ``pre`` survives as ``pre-wrap``).
_DROP_WRAP_PROPS = frozenset({"min-width", "overflow-wrap", "word-wrap", "word-break"})
_WRAPPING_WHITE_SPACE = {"nowrap": "normal", "pre": "pre-wrap"}
# A negative margin pulls text past the page edge just as ``left`` does.
_MARGIN_PREFIX = "margin"
_NEGATIVE_RE = re.compile(r"(?<![\w.])-(?=\.?\d)")
# Fixed widths are sized for a screen, not a Letter page: a 900 px table or
# float runs past the margin, and fixed column widths (Outlook writes them on
# every cell, newsletters nest them) add up past it once cells are nested or
# padded, while a ``width="100%"`` cell starves its neighbours to one
# character. Table cells lose every width and are sized by their content;
# other elements lose widths in absolute units or over 100%. Images keep
# theirs: the template caps them at the page width.
_CELL_TAGS = frozenset({"td", "th", "col", "colgroup"})
_KEEP_WIDTH_TAGS = frozenset({"img"})
_WIDTH_RE = re.compile(r"^\s*(\d*\.?\d+)\s*([a-z%]*)\s*(!\s*important)?\s*$", re.IGNORECASE)
# A run of this many characters with no space in it (a link target, a token,
# a URL typed into the text) is wrapped in a span that may break anywhere.
# Shorter runs, which include ordinary words and nearly every name and email
# address, only ever break at spaces, so Search & Redact still finds them.
LONG_RUN = 30
_LONG_RUN_RE = re.compile(r"[^ \t\n\r\f\v]{%d,}" % LONG_RUN)
_BREAKABLE_OPEN = '<span style="overflow-wrap: anywhere">'
_VOID_ELEMENTS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }
)
# Link targets that are never worth showing: they are not addresses a reader
# could follow on paper, or they are exactly the data we are trying not to leak.
_SILENT_SCHEMES = ("mailto:", "javascript:", "tel:", "cid:", "data:")


def _href_matches_text(href: str, text: str) -> bool:
    """True when the visible text already tells the reader where the link went."""

    def norm(s: str) -> str:
        s = s.strip().lower()
        s = re.sub(r"^(https?://|ftp://)", "", s)
        s = re.sub(r"^www\.", "", s)
        return s.rstrip("/")

    return norm(href) == norm(text) and norm(text) != ""


def _split_declarations(style: str) -> list[str]:
    """Split an inline style on ``;`` outside quotes and parentheses, so a
    ``url(data:image/png;base64,...)`` value stays in one piece."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for i, ch in enumerate(style):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            parts.append(style[start:i])
            start = i + 1
    parts.append(style[start:])
    return parts


def _drops_width(tag: str, value: str) -> bool:
    """Whether a ``width`` of *value* on *tag* goes (see ``_CELL_TAGS``)."""
    if tag in _KEEP_WIDTH_TAGS:
        return False
    if tag in _CELL_TAGS:
        return True
    m = _WIDTH_RE.match(value)
    if not m:  # auto, calc(), inherit: nothing fixed to drop
        return False
    return m.group(2) != "%" or float(m.group(1)) > 100


def clean_style(style: str, tag: str = "") -> str:
    """Drop the declarations in ``_DROP_STYLE_PROPS`` and ``_DROP_WRAP_PROPS``
    (and any ``-weasy-*``), negative margins and fixed widths (see
    ``_drops_width``; *tag* is the element the style is on) from an inline
    style, and make ``white-space`` wrap; everything else is kept as written."""
    kept: list[str] = []
    for decl in _split_declarations(style):
        decl = decl.strip()
        if not decl:
            continue
        prop, _, value = decl.partition(":")
        prop = prop.strip().lower()
        if (
            prop in _DROP_STYLE_PROPS
            or prop in _DROP_WRAP_PROPS
            or prop.startswith(_DROP_STYLE_PREFIX)
            or (prop.startswith(_MARGIN_PREFIX) and _NEGATIVE_RE.search(value))
            or (prop == "width" and _drops_width(tag, value))
        ):
            continue
        if prop == "white-space":
            value = value.replace("!important", "").strip().lower()
            decl = f"white-space: {_WRAPPING_WHITE_SPACE.get(value, value)}"
        kept.append(decl)
    return "; ".join(kept)


class _Cleaner(HTMLParser):
    """Re-serialize an HTML fragment, dropping what must not reach the PDF."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        # Stack of (tag, href, text_parts) for open anchors so ``[X]`` can be
        # appended after the visible text once the anchor closes.
        self._anchors: list[tuple[str | None, list[str], int]] = []
        # Depth counter for elements whose whole subtree is dropped.
        self._skip: list[str] = []
        self._textarea = 0

    # -- helpers ----------------------------------------------------------

    def _emit(self, s: str) -> None:
        if not self._skip:
            self._out.append(s)

    @staticmethod
    def _serialize_attrs(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        parts = []
        for name, value in attrs:
            name = name.lower()
            if name in _DROP_ATTRS or name.startswith("on"):
                continue
            if name == "width" and _drops_width(tag, value or ""):
                continue
            if name == "style" and value is not None:
                value = clean_style(value, tag)
                if not value:
                    continue
            if value is None:
                parts.append(f" {name}")
            else:
                parts.append(f' {name}="{escape(value, quote=True)}"')
        return "".join(parts)

    # -- HTMLParser callbacks ---------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in _DROP_WITH_CONTENT:
                self._skip.append(tag)
            return
        if tag in _DROP_WITH_CONTENT:
            self._skip.append(tag)
            return
        if tag in _DROP_TAG_ONLY:
            return
        if tag == "a":
            href = next((v for k, v in attrs if k.lower() == "href"), None)
            self._anchors.append((href, [], len(self._out)))
        elif tag == "textarea":
            self._textarea += 1
        self._out.append(f"<{tag}{self._serialize_attrs(tag, attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip or tag in _DROP_WITH_CONTENT or tag in _DROP_TAG_ONLY:
            return
        if tag == "a":
            # ``<a href="X"/>`` has no text; treat like an empty anchor.
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)
            return
        self._out.append(f"<{tag}{self._serialize_attrs(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag in _DROP_WITH_CONTENT or tag in _DROP_TAG_ONLY or tag in _VOID_ELEMENTS:
            return
        if tag == "a":
            self._close_anchor()
            return
        if tag == "textarea":
            self._textarea = max(0, self._textarea - 1)
        self._out.append(f"</{tag}>")

    def _close_anchor(self) -> None:
        if not self._anchors:
            return
        href, text_parts, _ = self._anchors.pop()
        suffix = self._link_suffix(href, "".join(text_parts))
        if suffix:
            self._out.append(self._text(suffix))
        self._out.append("</a>")

    @staticmethod
    def _link_suffix(href: str | None, text: str) -> str:
        if not href:
            return ""
        href = href.strip()
        if not href or href.startswith("#"):
            return ""
        if href.lower().startswith(_SILENT_SCHEMES):
            return ""
        if _href_matches_text(href, text):
            return ""
        return f" [{href}]" if text.strip() else f"[{href}]"

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        for _, text_parts, _ in self._anchors:
            text_parts.append(data)
        self._out.append(self._text(data))

    def _text(self, data: str) -> str:
        """Escape *data*, wrapping each long unbroken run in a breakable span
        (a textarea's content is raw text, so markup there would show)."""
        if self._textarea:
            return escape(data, quote=False)
        out: list[str] = []
        pos = 0
        for m in _LONG_RUN_RE.finditer(data):
            out.append(escape(data[pos:m.start()], quote=False))
            out.append(f"{_BREAKABLE_OPEN}{escape(m.group(), quote=False)}</span>")
            pos = m.end()
        out.append(escape(data[pos:], quote=False))
        return "".join(out)

    def handle_comment(self, data: str) -> None:  # drop, including <!--[if mso]>
        return

    def handle_decl(self, decl: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    def result(self) -> str:
        self.close()
        while self._anchors:  # unclosed <a> at end of input
            self._close_anchor()
        return "".join(self._out)


def clean_html(fragment: str) -> str:
    """Apply body hygiene to an HTML fragment.

    * ``<a href="X">text</a>`` keeps its text and gains `` [X]`` unless X is a
      ``mailto:`` (or similar) link or the text already reads as X. No
      ``href`` survives, so the renderer never emits a link annotation.
    * ``alt``, ``title``, ``id``, ``name`` and ``on*`` attributes are dropped
      from every element.
    * Inline ``style`` loses the declarations that hide or reposition text
      (``position``, offsets, ``opacity``, ``text-indent``, ``clip``, ...) and
      WeasyPrint's bookmark/string properties; see ``_DROP_STYLE_PROPS``.
      Text drawn in the background colour is not detected: that is what
      Acrobat's Sanitize step is for.
    * ``script``, ``style``, ``iframe``, ``object`` (with their content) and
      ``link``, ``meta``, ``form``, ``input`` (tags only) are removed, as are
      comments and declarations.

    Built on ``html.parser`` so it never raises on broken markup.
    """
    cleaner = _Cleaner()
    cleaner.feed(fragment)
    return cleaner.result()


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def normalize_message_id(value: str | None) -> str | None:
    """``"<abc@x>"`` -> ``"abc@x"``; None/blank stays None."""
    if not value:
        return None
    value = value.strip()
    match = _MSGID_RE.search(value)
    if match:
        value = match.group(1)
    value = value.strip().strip("<>").strip()
    return value or None


def _message_id_list(value: str | None) -> list[str]:
    """Split a References-style header into normalized ids, in order, deduped."""
    if not value:
        return []
    ids = _MSGID_RE.findall(value)
    if not ids:
        ids = value.split()
    seen: set[str] = set()
    out: list[str] = []
    for raw in ids:
        norm = normalize_message_id(raw)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _participants(*header_values: str) -> list[Participant]:
    """Parse From/To/Cc into Participants: names unquoted, addresses lowercased."""
    out: list[Participant] = []
    seen: set[tuple[str, str]] = set()
    for name, addr in getaddresses([v for v in header_values if v]):
        name = name.strip().strip('"').strip()
        addr = addr.strip().lower()
        if not name and not addr:
            continue
        key = (name, addr)
        if key in seen:
            continue
        seen.add(key)
        out.append(Participant(name=name, address=addr))
    return out


def content_hash(
    date: datetime | None, from_addr: str, to_addrs: str, subject: str, body_html: str
) -> str:
    """sha256 over ``date|from|to|subject|sha256(body_html)``."""
    body_digest = hashlib.sha256(body_html.encode("utf-8")).hexdigest()
    date_str = date.isoformat() if date else ""
    joined = "|".join([date_str, from_addr, to_addrs, subject, body_digest])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# .eml parsing
# ---------------------------------------------------------------------------


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def _inner_html(html: str) -> str:
    """Reduce a full HTML document to a fragment safe to embed in our template."""
    match = _BODY_RE.search(html)
    if match:
        return match.group(1)
    return _STRIP_RE.sub("", html)


def stable_readpst_ics(
    filename: str, content: bytes, date: datetime | None
) -> tuple[str, bytes]:
    """Make a readpst-synthesized ``i<rand>.ics`` the same on every extraction.

    The file is recognized by both readpst's name and its ``PRODID:LibPST``
    line; anything else, including every .ics a sender attached, is returned
    untouched. The name becomes ``READPST_ICS_NAME`` and DTSTAMP becomes the
    message's Date (the time the request was sent, which is what DTSTAMP means
    in a meeting request), else the file's own CREATED, else the line goes.
    The bytes are rewritten rather than hashed around, so the sha256 in the
    index, manifest and survey is the sha256 of the native file on disk.
    DTSTAMP is never rendered and names no one.
    """
    if not _READPST_ICS_NAME_RE.fullmatch(filename) or not _READPST_ICS_PRODID_RE.search(
        content
    ):
        return filename, content
    stamp: bytes | None = None
    if date is not None:
        utc = date.astimezone(timezone.utc) if date.tzinfo else date
        stamp = utc.strftime("%Y%m%dT%H%M%SZ").encode("ascii")
    elif created := _CREATED_RE.search(content):
        stamp = created.group(1)
    content = _DTSTAMP_RE.sub(
        lambda m: b"DTSTAMP:" + stamp + m.group(1) if stamp else b"", content, count=1
    )
    return READPST_ICS_NAME, content


def _header(msg: EmailMessage, name: str) -> str:
    try:
        value = msg[name]
    except Exception:  # pragma: no cover - defensive against malformed headers
        return ""
    return str(value) if value else ""


def parse_eml(path: Path, folder: str = "", *, readpst_compat: bool = True) -> MessageModel:
    with path.open("rb") as fh:
        msg: EmailMessage = BytesParser(policy=policy.default).parse(fh)

    return parse_message(msg, path, folder, readpst_compat=readpst_compat)


def parse_message(msg: EmailMessage, path: Path, folder: str = "", *, readpst_compat: bool = True) -> MessageModel:
    """Shared MIME parsing for readpst EML and offline MBOX intake."""
    date: datetime | None
    try:
        date = parsedate_to_datetime(msg["Date"])
    except (TypeError, ValueError, OverflowError):
        date = None

    body_part = msg.get_body(preferencelist=("html", "plain"))
    if body_part is None:
        body_html = "<p><em>(no message body)</em></p>"
    elif body_part.get_content_type() == "text/html":
        body_html = _inner_html(_part_text(body_part))
    else:
        body_html = f"<pre>{escape(_part_text(body_part))}</pre>"

    # Re-embed inline images referenced as cid: URLs so they survive into the
    # PDF without any network access.
    inline_cids: set[str] = set()
    has_images = bool(_IMG_RE.search(body_html))  # data: images already in the source
    for part in _outer_parts(msg):
        cid = (part["Content-ID"] or "").strip("<>")
        if not cid or part.is_multipart():
            continue
        if f"cid:{cid}" in body_html:
            payload = part.get_payload(decode=True) or b""
            content_type = part.get_content_type()
            if images.is_visible(images.probe(payload)):
                has_images = True
            payload, content_type = images.normalize(payload, content_type)
            data_uri = f"data:{content_type};base64,{base64.b64encode(payload).decode('ascii')}"
            body_html = body_html.replace(f"cid:{cid}", data_uri)
            inline_cids.add(cid)

    body_html = clean_html(body_html)

    attachments: list[Attachment] = []
    had_rtf_body = False
    for part in _outer_parts(msg):
        if (part.is_multipart() and part.get_content_type() != "message/rfc822") or part is body_part:
            continue
        if (part["Content-ID"] or "").strip("<>") in inline_cids:
            continue
        filename = part.get_filename()
        if not filename and part.get_content_disposition() != "attachment" and not part["Content-ID"] and part.get_content_type() != "message/rfc822":
            continue
        if filename == RTF_BODY_NAME and readpst_compat:
            had_rtf_body = True
            log.debug("%s: RTF body attachment skipped", path.name)
            continue
        if part.get_content_type() == "message/rfc822":
            nested = part.get_payload()
            content = b"\n".join(item.as_bytes(policy=policy.default) for item in nested) if isinstance(nested, list) else (part.get_payload(decode=True) or b"")
            filename = filename or "Attached message.eml"
        else:
            content = part.get_payload(decode=True) or b""
        if readpst_compat:
            filename, content = stable_readpst_ics(filename or "attachment", content, date)
        attachments.append(
            Attachment(
                filename=filename,
                content=content,
                content_type=part.get_content_type(),
            )
        )

    subject = _header(msg, "Subject")
    from_addr = _header(msg, "From")
    to_addrs = _header(msg, "To")
    cc_addrs = _header(msg, "Cc")

    return MessageModel(
        source=path,
        folder=folder,
        subject=subject,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=_header(msg, "Bcc"),
        original_date=next((v for k, v in msg.raw_items() if k.lower() == "date"), ""),
        original_timestamp=date.isoformat() if date else None,
        date=date,
        body_html=body_html,
        attachments=attachments,
        kind="mail",
        message_id=normalize_message_id(_header(msg, "Message-ID")),
        in_reply_to=next(iter(_message_id_list(_header(msg, "In-Reply-To"))), None),
        references=_message_id_list(_header(msg, "References")),
        thread_index=_header(msg, "Thread-Index").strip() or None,
        participants=_participants(from_addr, to_addrs, cc_addrs, _header(msg, "Bcc")),
        has_images=has_images,
        had_rtf_body=had_rtf_body,
        content_hash=content_hash(date, from_addr, to_addrs, subject, body_html),
    )


def _outer_parts(msg):
    """Walk the outer MIME tree without promoting forwarded mail's parts."""
    yield msg
    if msg.is_multipart() and msg.get_content_type() != "message/rfc822":
        for child in msg.iter_parts():
            yield from _outer_parts(child)
