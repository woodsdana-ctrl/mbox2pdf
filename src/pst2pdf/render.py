"""Stage 3: render a MessageModel (or any report page) to PDF via Jinja2 + WeasyPrint.

Every visible page in the deliverable comes through here, so this is where
the text-layer rules live: one embedded font, no ligatures, no hyphenation,
no PDF bookmarks or link annotations generated from the HTML. The templates
extend ``_base.html`` to inherit that CSS.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .parse import MessageModel

log = logging.getLogger(__name__)

_env: Environment | None = None

# Rendered documents that still had text or an image outside the page, for
# the renders that can only log it (attachments, report pages). Counted in the
# parent process by note_offpage, never in render_pdf, which may run in a
# worker whose globals never reach the parent. cli.build_volumes resets it
# when a run starts and reads it when the run ends.
offpage_count = 0

_NAME_ADDR_RE = re.compile(r"^(.*?)\s*<([^<>]*)>\s*$")


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def thousands(num: int | None) -> str:
    """1320 -> "1,320"; None -> "0"."""
    return f"{num or 0:,}"


def split_addresses(header: str) -> list[str]:
    """Break a From/To/Cc header into one string per mailbox.

    Each entry is rendered as ``Name <address>`` (or whichever half exists)
    inside a ``white-space: nowrap`` span, so a long recipient list wraps only
    between people and Acrobat's search sees every name on one line. Quoted
    display names lose their quotes; nothing else is normalized, because the
    page should show what the header said. The split is on commas outside
    quotes and angle brackets rather than ``email.utils.getaddresses``, which
    drops or duplicates entries when a real-world header is slightly
    malformed; here nothing is ever lost.
    """
    pieces: list[str] = []
    buf: list[str] = []
    in_quotes = False
    depth = 0
    for ch in header or "":
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            if ch in "<(":
                depth += 1
            elif ch in ">)":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                pieces.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    pieces.append("".join(buf))

    out: list[str] = []
    for piece in pieces:
        piece = " ".join(piece.split())
        if not piece:
            continue
        m = _NAME_ADDR_RE.match(piece)
        if m:
            name = m.group(1).strip().strip('"').strip()
            addr = m.group(2).strip()
            if not name or name.lower() == addr.lower():
                piece = addr or name
            else:
                piece = f"{name} <{addr}>"
        else:
            piece = piece.strip('"').strip()
        if piece:
            out.append(piece)
    return out


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=PackageLoader("pst2pdf"),
            autoescape=select_autoescape(["html"]),
        )
        _env.filters["human_size"] = human_size
        _env.filters["thousands"] = thousands
        _env.filters["addresses"] = split_addresses
    return _env


def build_template(name: str, **context) -> str:
    """Render template *name* (a file under ``templates/``) to an HTML string."""
    return _get_env().get_template(name).render(**context)


def build_html(
    model: MessageModel,
    seq: int | None = None,
    folders: list[str] | None = None,
    copies: int = 1,
    notice: str | None = None,
    body_html: str | None = None,
) -> str:
    """HTML for one message page.

    *seq* is the message's number in the final output order and *folders*
    every PST folder it was found in (more than one after message dedup).
    Only a triage copy passes them (see ``cli._header_origin``); without them
    the header says nothing about where the message sat in the archive.
    *notice* and *body_html* are used by the render fallbacks (see
    :func:`fallback_html`).
    """
    return build_template(
        "message.html", msg=model, seq=seq, folders=folders or [], copies=copies, notice=notice, body_html=body_html
    )


TEXT_FALLBACK_NOTICE = "The formatting of this message could not be rendered. Its text is shown unformatted."
OFFPAGE_FALLBACK_NOTICE = (
    "The formatting of this message would have placed some of its text beyond the edge of the page. "
    "Its text is shown unformatted."
)
HEADER_FALLBACK_NOTICE = "The body of this message could not be rendered."

# Paragraph-like tags get a blank line around them; line-like tags a line break.
_PARA_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table",
    "ul", "ol", "section", "article", "header", "footer",
}
_LINE_TAGS = {"br", "tr", "li", "hr", "dd", "dt"}
_SKIP_TAGS = {"script", "style", "head", "title"}


class _TextExtractor(HTMLParser):
    """Visible text of an HTML fragment, block boundaries as line breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _PARA_TAGS:
            self.parts.append("\n\n")
        elif tag in ("br", "hr"):
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _PARA_TAGS:
            self.parts.append("\n\n")
        elif tag in _LINE_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(fragment: str) -> str:
    """Plain text of *fragment*: tags stripped, entities decoded, blank runs collapsed."""
    parser = _TextExtractor()
    parser.feed(fragment or "")
    parser.close()
    text = "".join(parser.parts).replace("\r", "")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def fallback_html(
    model: MessageModel,
    level: str,
    seq: int | None = None,
    folders: list[str] | None = None,
    copies: int = 1,
) -> str:
    """The page rendered when the real one fails.

    ``level == "text"`` keeps the header and shows the body as escaped plain
    text with a notice; ``"offpage"`` does the same with a notice saying the
    formatted page would have hidden text past its edge; ``"header"`` shows
    the header and the notice alone. A message must never vanish from the
    volumes because its HTML was odd.
    """
    if level in ("text", "offpage"):
        body = f"<pre>{escape(html_to_text(model.body_html))}</pre>"
        notice = OFFPAGE_FALLBACK_NOTICE if level == "offpage" else TEXT_FALLBACK_NOTICE
        return build_html(model, seq, folders, copies, notice=notice, body_html=body)
    return build_html(model, seq, folders, copies, notice=HEADER_FALLBACK_NOTICE, body_html="")


def _make_offline_fetcher():
    """Return (fetcher, blocked_counter) for whichever WeasyPrint API is installed.

    Inline images arrive as ``data:`` URIs; anything else would be a network
    fetch, which we refuse (the container should work fully offline and
    remote trackers in mail bodies should never be hit). Blocked fetches are
    counted, not treated as errors: WeasyPrint catches the exception and
    renders the document without that resource.

    WeasyPrint 70 replaced the ``default_url_fetcher`` function with a
    ``URLFetcher`` class whose ``fetch`` method must be overridden; earlier
    versions take any callable. Both are supported so the image keeps working
    across dependency upgrades.
    """
    blocked = [0]

    def refuse(url: str):
        blocked[0] += 1
        log.debug("blocked remote resource: %s", url)
        raise ValueError(f"external resource blocked: {url}")

    try:
        from weasyprint.urls import URLFetcher  # WeasyPrint >= 70
    except ImportError:
        from weasyprint import default_url_fetcher  # WeasyPrint < 70

        def fetcher(url: str, *args, **kwargs):
            if url.startswith("data:"):
                return default_url_fetcher(url, *args, **kwargs)
            refuse(url)

        return fetcher, blocked

    class OfflineFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            if url.startswith("data:"):
                return super().fetch(url, headers)
            refuse(url)

    return OfflineFetcher(), blocked


def _min_content_breaks_anywhere() -> None:
    """Make WeasyPrint's min-content width honour ``overflow-wrap: anywhere``
    for every text box, not only the ones that start a line.

    A table is never laid out narrower than the sum of its cells' min-content
    widths, and WeasyPrint (checked on 70) lets a word break inside that
    measurement only when its text box is the first thing on the line. A
    link target that follows an empty inline (a blocked remote image, say)
    therefore counts at full length, the table grows past the page, and the
    text beyond the margin is clipped from view but kept in the text layer:
    a long link target can run to thousands of pixels on an 816 px page.
    During that measurement every call asks for the narrowest possible line
    (``max_width == 0``), so treating each one as a line start is exactly the
    CSS definition of min-content under ``anywhere``. Real line layout is not
    touched. If a future WeasyPrint moves the function, this does nothing and
    the off-page check in :func:`render_pdf` still reports the result.
    """
    try:
        from weasyprint.layout import preferred

        original = preferred.split_first_line
    except (ImportError, AttributeError):  # pragma: no cover - other WeasyPrint layouts
        log.debug("WeasyPrint min-content patch not applied")
        return
    if getattr(original, "_pst2pdf", False):
        return

    def split_first_line(text, style, context, max_width, *args, **kwargs):
        minimum = kwargs.get("minimum", args[2] if len(args) > 2 else False)
        if minimum and max_width == 0 and style["overflow_wrap"] == "anywhere":
            if len(args) > 1:
                args = (args[0], True, *args[2:])
            else:
                kwargs["is_line_start"] = True
        return original(text, style, context, max_width, *args, **kwargs)

    split_first_line._pst2pdf = True
    preferred.split_first_line = split_first_line


# Layout rounding leaves text a fraction of a pixel past a margin; the check is
# for content that is actually cut off.
_OFFPAGE_TOLERANCE = 1.0  # CSS px


def offpage_boxes(document) -> int:
    """How many text and image boxes of a laid-out WeasyPrint *document* lie,
    even partly, outside their page.

    Such content is clipped from view but still written to the PDF: text
    stays in the text layer (Search & Redact finds it, the reviewer never sees
    it) and an image keeps its full pixels. Nothing in a rendered page should
    ever be there. Returns 0 when the layout tree is not available.
    """
    count = 0
    for page in document.pages:
        root = getattr(page, "_page_box", None)
        if root is None:  # pragma: no cover - WeasyPrint internals changed
            return 0
        right = page.width + _OFFPAGE_TOLERANCE
        bottom = page.height + _OFFPAGE_TOLERANCE
        stack = [root]
        while stack:
            box = stack.pop()
            text = getattr(box, "text", None)
            is_image = getattr(box, "replacement", None) is not None
            if (text and text.strip()) or is_image:
                if (
                    box.position_x < -_OFFPAGE_TOLERANCE
                    or box.position_y < -_OFFPAGE_TOLERANCE
                    or box.position_x + box.width > right
                    or box.position_y + box.height > bottom
                ):
                    count += 1
            stack.extend(getattr(box, "children", None) or ())
    return count


@dataclass(frozen=True)
class Rendered:
    """What :func:`render_pdf` reports: remote resources refused, and text or
    image boxes left outside the page (0 unless the layout could not fit)."""

    blocked: int = 0
    offpage: int = 0


def render_pdf(html: str, out_path: Path) -> Rendered:
    """Write *html* to *out_path* as PDF and report what the layout found.

    This is the unit of work handed to render worker processes, so it must
    stay a module-level function with picklable arguments and result. The
    caller adds the counts to the run's totals: a worker's globals never reach
    the parent process.
    """
    # WeasyPrint is imported lazily: it needs Pango/Cairo system libraries,
    # which are guaranteed in the Docker image but not on a dev host.
    from weasyprint import HTML

    _min_content_breaks_anywhere()
    fetcher, blocked = _make_offline_fetcher()
    document = HTML(string=html, url_fetcher=fetcher).render()
    offpage = offpage_boxes(document)
    document.write_pdf(str(out_path))
    return Rendered(blocked[0], offpage)


def note_offpage(result: Rendered, what: str) -> None:
    """Log and count (in ``offpage_count``) a render that left text or an
    image outside the page. Call it in the parent process."""
    global offpage_count
    if result.offpage:
        offpage_count += 1
        log.warning("%s: %d text or image boxes lie outside the page edge", what, result.offpage)


def render_checked(html: str, out_path: Path) -> Rendered:
    """:func:`render_pdf` in this process, with :func:`note_offpage`."""
    result = render_pdf(html, out_path)
    note_offpage(result, Path(out_path).name)
    return result


def render_template(name: str, out_path: Path, **context) -> Path:
    """Render template *name* with *context* straight to a PDF at *out_path*.

    Convenience for the report pages (cover sheets, index, START HERE), which
    are rendered in the parent process and never need the blocked-URL count.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_checked(build_template(name, **context), out_path)
    return out_path
