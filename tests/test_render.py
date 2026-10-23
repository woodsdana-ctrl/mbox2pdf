import re
from html import unescape
from pathlib import Path

import pytest

from pst2pdf import render
from pst2pdf.parse import parse_eml

from conftest import PNG_BYTES, make_message, write_eml
from conftest import make_model as build_model
from test_report import make_model, sample_bundles


def _weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
        return True
    except Exception:
        return False


needs_weasyprint = pytest.mark.skipif(
    not _weasyprint_available(), reason="WeasyPrint system libraries not installed"
)


def _text(html: str) -> str:
    html = re.sub(r"<style>.*?</style>", " ", html, flags=re.DOTALL)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def test_build_html(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(attachments=[("notes.txt", b"hi there")]), path)
    html = render.build_html(parse_eml(path))

    assert "Quarterly report" in html
    assert "alice@example.com" in html
    assert "notes.txt" in html
    assert "8 B" in html
    assert "<p>Hello <b>world</b></p>" in html
    # Legacy call: no message number, no folder line, no running footer.
    assert "Message " not in _text(html)
    assert "@bottom-left" not in html
    assert "Folder" not in html


def test_build_html_hygiene_css():
    html = render.build_html(make_model(1))
    assert "font-variant-ligatures: none !important" in html
    assert "hyphens: none !important" in html
    assert 'font-family: "DejaVu Sans", "Noto Sans CJK SC" !important' in html
    assert "bookmark-level: none" in html
    assert "<title>" not in html
    assert "size: Letter;" in html


def test_build_html_message_number_and_folders():
    html = render.build_html(make_model(3, folder="Inbox/Projects"), seq=17, folders=["Inbox/Projects", "Sent Items"])
    text = _text(html)
    assert "Message 17 · Folders: Inbox/Projects, Sent Items" in text
    # The seq lives in the header block only; nothing is printed in the footer.
    assert "@bottom-left" not in html
    # The model's own folder is never shown unless the caller passes it.
    html = render.build_html(make_model(3, folder="Inbox"), seq=2)
    assert "Message 2" in _text(html) and "Folder" not in html


def test_build_html_addresses_do_not_wrap_inside_a_name():
    model = make_model(
        1,
        to_addrs='Bob Example <bob@example.com>, "Example, Heidi" <heidi@example.com>',
        cc_addrs="carol@example.com",
    )
    html = render.build_html(model)
    assert '<span class="addr">Bob Example &lt;bob@example.com&gt;</span>, ' in html
    assert '<span class="addr">Example, Heidi &lt;heidi@example.com&gt;</span>' in html
    assert '<span class="addr">carol@example.com</span>' in html
    assert ".addr { white-space: nowrap; overflow-wrap: normal; }" in html


def test_split_addresses():
    assert render.split_addresses("") == []
    assert render.split_addresses("alice@example.com") == ["alice@example.com"]
    assert render.split_addresses('"Alice Example" <alice@example.com>') == ["Alice Example <alice@example.com>"]
    assert render.split_addresses("alice@example.com <alice@example.com>") == ["alice@example.com"]
    # Shown as the header said it, never dropped or duplicated.
    assert render.split_addresses("Undisclosed recipients:;") == ["Undisclosed recipients:;"]
    assert render.split_addresses('"Doe, Jane" <jane@example.com>,  , John Roe <john@example.com>') == [
        "Doe, Jane <jane@example.com>",
        "John Roe <john@example.com>",
    ]
    assert render.split_addresses("Jane (Admin) <jane@example.com>") == ["Jane (Admin) <jane@example.com>"]


def test_build_html_calendar_layout():
    html = render.build_html(make_model(2, subject="Budget review", kind="calendar"), seq=2, folders=["Calendar"])
    text = _text(html)
    assert "Calendar item" in text
    assert "Organizer" in text and "Attendees" in text and "When" in text
    assert "<th>From</th>" not in html and "<th>To</th>" not in html


def test_build_html_calendar_omits_empty_rows():
    model = build_model(subject="Budget review", kind="calendar", from_addr="", to_addrs="")
    html = render.build_html(model, seq=2)
    assert "Organizer" not in html and "Attendees" not in html
    assert "<th>When</th>" in html
    # Mail keeps its From/To rows even when empty, so a missing header is visible.
    html = render.build_html(build_model(from_addr="", to_addrs=""), seq=1)
    assert "<th>From</th>" in html and "<th>To</th>" in html


def test_build_html_date_shows_zone_name():
    html = render.build_html(build_model(date="2025-09-16T10:17:00-05:00"))
    # A fixed-offset zone has no short name; datetime prints UTC-05:00 for it.
    assert "Tue, 16 Sep 2025 10:17 UTC-05:00" in _text(html)
    from zoneinfo import ZoneInfo

    html = render.build_html(build_model(date=__import__("datetime").datetime(2025, 9, 16, 10, 17, tzinfo=ZoneInfo("America/Chicago"))))
    assert "Tue, 16 Sep 2025 10:17 CDT" in _text(html)


def test_html_to_text():
    html = (
        "<div><p>Hello <b>world</b> &amp; friends</p><style>p{}</style>"
        "<table><tr><td>a</td><td>b</td></tr></table><ul><li>one</li><li>two</li></ul>"
        "<p>Line<br>break</p>\n\n\n<p>  spaced   out  </p></div>"
    )
    assert render.html_to_text(html) == "Hello world & friends\n\na b\n\none\ntwo\n\nLine\nbreak\n\nspaced out"
    assert render.html_to_text("") == ""


def test_fallback_html_levels():
    model = build_model(subject="Quarterly budget", body_html="<p>Please <b>affirm</b> &lt;this&gt;</p><script>x</script>")
    text_page = render.fallback_html(model, "text", seq=3, folders=["Inbox"])
    t = _text(text_page)
    assert "Message 3 · Folder: Inbox" in t
    assert "Quarterly budget" in t and "alice@example.com" in t
    assert render.TEXT_FALLBACK_NOTICE in t
    assert "<pre>Please affirm &lt;this&gt;</pre>" in text_page
    assert "<b>" not in text_page.split('class="body"')[1]
    header_page = render.fallback_html(model, "header", seq=3)
    h = _text(header_page)
    assert render.HEADER_FALLBACK_NOTICE in h
    assert "affirm" not in h
    assert 'class="body"></div>' in header_page


def test_build_template_and_filters():
    assert render.thousands(1320) == "1,320"
    assert render.thousands(None) == "0"
    html = render.build_template("message.html", msg=make_model(1), seq=None, folders=[])
    assert "Quarterly budget" in html


def test_human_size():
    assert render.human_size(8) == "8 B"
    assert render.human_size(2048) == "2.0 KB"
    assert render.human_size(5 * 1024 * 1024) == "5.0 MB"


@needs_weasyprint
def test_render_pdf(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(), path)
    out = tmp_path / "out.pdf"
    render.render_pdf(render.build_html(parse_eml(path)), out)

    assert out.read_bytes().startswith(b"%PDF")


@needs_weasyprint
def test_render_pdf_blocks_remote_resources(tmp_path):
    html = (
        '<html><body><p>hi</p>'
        '<img src="https://example.invalid/tracker.gif">'
        '<img src="http://example.invalid/logo.png">'
        f'<img src="data:image/png;base64,{__import__("base64").b64encode(PNG_BYTES).decode()}">'
        "</body></html>"
    )
    out = tmp_path / "out.pdf"
    result = render.render_pdf(html, out)

    assert out.read_bytes().startswith(b"%PDF")
    assert result.blocked == 2
    assert result.offpage == 0


@needs_weasyprint
def test_render_template_writes_pdf(tmp_path):
    out = render.render_template("message.html", tmp_path / "sub" / "m.pdf", msg=make_model(1), seq=1, folders=["Inbox"])
    assert out.read_bytes().startswith(b"%PDF")


@needs_weasyprint
def test_rendered_text_layer_is_clean(tmp_path):
    """No ligature glyphs, no link annotations, no outline, no title, one font."""
    from pypdf import PdfReader

    bundles = sample_bundles()
    b3 = bundles[2]
    out = tmp_path / "m.pdf"
    render.render_pdf(render.build_html(b3.model, seq=3, folders=b3.folders), out)
    reader = PdfReader(str(out))
    text = "".join(p.extract_text() for p in reader.pages)
    assert "office" in text and "file" in text and "affirm" in text
    assert "ﬁ" not in text and "ﬂ" not in text and "ﬀ" not in text
    assert "difficult" in text
    for page in reader.pages:
        assert "/Annots" not in page or not [a for a in page["/Annots"] if a.get_object().get("/Subtype") == "/Link"]
    assert reader.outline == []
    assert not (reader.metadata or {}).get("/Title")
    # One family only (regular and bold faces of DejaVu Sans), nothing pulled
    # in by the mail body's own font-family declarations.
    fonts = set()
    for page in reader.pages:
        for f in page["/Resources"].get("/Font", {}).values():
            fonts.add(str(f.get_object()["/BaseFont"]).split("+")[-1])
    assert fonts and all(name.startswith("DejaVu-Sans") or name.startswith("DejaVuSans") for name in fonts), fonts


_NOTO_CJK = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
needs_cjk_font = pytest.mark.skipif(not _NOTO_CJK.exists(), reason="Noto Sans CJK is installed in the image only")

CJK = "於 2026年7月24日週五 上午 寫道：此邮件已验证 東京都 서울특별시"


def _fonts_and_text(path: Path) -> tuple[set[str], set[str], str]:
    """Base font names, the names of any font missing glyphs (WeasyPrint gives
    those an embedded CMap in place of /Identity-H), and the page text."""
    import pdfplumber
    from pypdf import PdfReader

    from pst2pdf import premark  # noqa: F401  (reads the embedded CMaps too)

    names, missing = set(), set()
    for page in PdfReader(str(path)).pages:
        for f in page["/Resources"].get("/Font", {}).values():
            f = f.get_object()
            name = str(f["/BaseFont"]).split("+")[-1]
            names.add(name)
            if f["/Encoding"] != "/Identity-H":
                missing.add(name)
    with pdfplumber.open(path) as pdf:
        text = " ".join(p.extract_text() for p in pdf.pages)
    return names, missing, text


@needs_weasyprint
@needs_cjk_font
def test_cjk_in_a_message_is_drawn_in_noto_not_as_boxes(tmp_path):
    """DejaVu Sans has no CJK glyphs; they were drawn as .notdef boxes. Noto
    Sans CJK is the one fallback, and a body's own CJK font is still ignored."""
    model = build_model(body_html=f'<p style="font-family: SimSun, \'MS Mincho\'">Jane Doe {CJK}</p><p><b>粗体 {CJK}</b></p>')
    out = tmp_path / "m.pdf"
    render.render_pdf(render.build_html(model, seq=1, folders=["Inbox"]), out)
    names, missing, text = _fonts_and_text(out)
    assert not missing, missing
    assert names == {"DejaVu-Sans", "DejaVu-Sans-Bold", "Noto-Sans-CJK-SC", "Noto-Sans-CJK-SC-Bold"}, names
    assert CJK in " ".join(text.split())


@needs_weasyprint
@needs_cjk_font
def test_cjk_in_a_text_attachment_is_drawn_in_noto_mono(tmp_path):
    from html import escape

    from pst2pdf import convert

    out = tmp_path / "t.pdf"
    render.render_pdf(convert._wrap_html(f"<pre>{escape('ID 0042  ' + CJK)}</pre>"), out)
    names, missing, text = _fonts_and_text(out)
    assert not missing, missing
    assert names == {"DejaVu-Sans-Mono", "Noto-Sans-Mono-CJK-SC"}, names
    assert CJK in " ".join(text.split())


_NOTO_EMOJI = Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")
needs_emoji_font = pytest.mark.skipif(not _NOTO_EMOJI.exists(), reason="Noto Color Emoji is installed in the image only")


@needs_weasyprint
@needs_emoji_font
def test_emoji_are_drawn_and_searchable_and_leave_digits_alone(tmp_path):
    """Emoji come from Noto Color Emoji by fallback: drawn as images over an
    outline-free glyph, so each is still one character in the text layer.
    The font must never be named in the CSS: it has digits and would take
    over dates and Bates numbers."""
    import pdfplumber
    from pypdf import PdfReader

    body = "<p>Status ✅ done 👋🎉 👍🏽 👨\u200d👩\u200d👧 🇺🇸 Jane Doe, 15 March 2026 #4 ❤️</p><p>© ☎ ♥ ✔ ★ 0123456789</p>"
    out = tmp_path / "m.pdf"
    render.render_pdf(render.build_html(build_model(body_html=body), seq=1, folders=["Inbox"]), out)
    names, missing, text = _fonts_and_text(out)
    assert not missing, missing
    assert "Noto-Color-Emoji" in names, names
    flat = " ".join(text.split())
    assert "Status ✅ done 👋🎉 👍🏽 👨\u200d👩\u200d👧 🇺🇸 Jane Doe, 15 March 2026 #4" in flat
    page = PdfReader(str(out)).pages[0]
    assert len(page["/Resources"].get("/XObject", {})) >= 6  # one image per distinct emoji glyph
    with pdfplumber.open(out) as pdf:
        chars = pdf.pages[0].chars
    by_text = {}
    for c in chars:
        by_text.setdefault(c["text"], set()).add(c["fontname"].split("+")[-1])
    for ch in "0123456789#©☎♥✔★":  # text characters stay in DejaVu, in the header and the body
        assert by_text.get(ch, set()) and all(f.startswith("DejaVu") for f in by_text[ch]), (ch, by_text.get(ch))


def test_the_emoji_font_is_not_named_in_the_css():
    html = render.build_html(build_model())
    assert "Emoji" not in html.split("</style>")[0]


@needs_emoji_font  # i.e. running in the image
def test_the_images_font_cache_is_valid_so_no_process_writes_its_own(tmp_path):
    """fontconfig trusts a cache only while each font directory's mtime matches
    the recorded one to the nanosecond; an image layer keeps whole seconds, so
    the image's cache was stale in every container. Each render process then
    wrote its own under $HOME, and processes starting together raced to create
    the directory: "Fontconfig error: No writable cache directories"."""
    import subprocess

    proc = subprocess.run(
        ["fc-list"], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0 and "Noto Color Emoji" in proc.stdout
    assert "Fontconfig error" not in proc.stderr
    written = list((tmp_path / ".cache").rglob("*cache-*"))
    assert written == [], f"fontconfig rebuilt {len(written)} directory caches: the image's cache is stale"


# ---------------------------------------------------------------------------
# Body content stays on the page
# ---------------------------------------------------------------------------

# A newsletter laid out for a 700 px screen: fixed-width nested tables whose
# cells add up past the page, a no-wrap cell, a link whose only content is a
# (blocked) remote image so its target follows an empty inline, a no-wrap
# span, and a preformatted line. Before the fix, text in it was clipped past
# the right edge and survived only in the text layer.
NEWSLETTER = """<html><body>
<table width="700" style="width:700px; border-radius:4px; overflow:hidden" align="center"><tr><td>
 <table width="100%"><tr>
  <td width="600" style="width:6.25in; padding:0 18pt">
   <p>Weekly Update: <span style="white-space:nowrap">Director Marisol Farrightington presents the budget update to the board on Thursday evening</span></p>
   <p><a href="https://urldefense.com/v3/__https://newsletter.example.com/weekly-update-2026-09?utm_source=email&amp;utm_medium=newsletter__;!!AAAAAAAAAAAA!abcdefghijklmnop">Read more</a></p>
  </td>
  <td width="400" style="width:300pt" nowrap>Contact Kristin Quillfeatherstone at 612-555-0100 for the agenda</td>
 </tr><tr>
  <td width="120"><a href="https://click.example.com/?qs=ExampleClickTokenExampleClickTokenExampleClickTokenExampleClickTokenExampleClickToken"><span><img src="https://example.invalid/logo.png" width="109"></span></a></td>
  <td width="600"><pre style="white-space:pre">a preformatted line that goes on and on well past the edge of any Letter page at this size: Zebulonhartwell</pre></td>
 </tr></table>
</td></tr></table>
</body></html>"""

NAMES = ("Director", "Farrightington", "Quillfeatherstone", "Zebulonhartwell", "612-555-0100")


def _words_off_page(pdf_path):
    import pdfplumber

    with pdfplumber.open(str(pdf_path)) as pdf:
        words = [(w, page) for page in pdf.pages for w in page.extract_words()]
    off = [w["text"] for w, p in words if w["x0"] < 0 or w["x1"] > float(p.width) or w["top"] < 0 or w["bottom"] > float(p.height)]
    return off, [w["text"] for w, _ in words]


@needs_weasyprint
def test_newsletter_table_stays_on_the_page(tmp_path):
    path = tmp_path / "news.eml"
    write_eml(make_message(subject="Weekly Update", html=NEWSLETTER), path)
    out = tmp_path / "news.pdf"
    result = render.render_pdf(render.build_html(parse_eml(path), seq=1), out)

    assert result.offpage == 0
    off, words = _words_off_page(out)
    assert off == []
    # Every name is on the page, whole, where Search & Redact finds it.
    for name in NAMES:
        assert any(name in w for w in words), name
    # Only the long link targets may break mid-run; the names do not.
    assert "Farrightington" in words and "Quillfeatherstone" in words


@needs_weasyprint
def test_offpage_boxes_counts_clipped_text(tmp_path):
    # Straight to render_pdf, skipping parse.clean_html: 14 columns of words
    # that cannot break are wider than a Letter page whatever the CSS says.
    row = "".join(f"<td>Column{i}Heading</td>" for i in range(14))
    html = f'<html><body><div class="body"><table><tr>{row}</tr></table></div></body></html>'
    result = render.render_pdf(html, tmp_path / "wide.pdf")
    assert result.offpage > 0
    off, _ = _words_off_page(tmp_path / "wide.pdf")
    assert off


@needs_weasyprint
def test_message_that_cannot_fit_is_shown_as_text(tmp_path):
    from pst2pdf import cli
    from pst2pdf.model import Options

    row = "".join(f"<td>Column{i}Heading</td>" for i in range(14))
    extract_root = tmp_path / "extract" / "Inbox"
    extract_root.mkdir(parents=True)
    write_eml(make_message(subject="Wide", html=f"<html><body><table><tr>{row}</tr></table></body></html>"), extract_root / "1.eml")
    write_eml(make_message(subject="Weekly Update", html=NEWSLETTER), extract_root / "2.eml")
    out = tmp_path / "out"
    stats = cli.build_volumes(tmp_path / "extract", out, Options(request_id="R", jobs=1, stamp_bates=False), tmp_path / "work")

    assert stats["errors"] == 0
    assert stats["offpage_fallbacks"] == 1  # the 14-column table; the newsletter fits
    assert stats["offpage_text"] == 0
    manifest = __import__("json").loads((out / "manifest.json").read_text())
    assert manifest["counts"]["offpage_fallbacks"] == 1
    assert manifest["counts"]["offpage_text"] == 0
    off, words = _words_off_page(out / "Volume 01 of 01.pdf")
    assert off == []
    assert "Column13Heading" in words
    text = " ".join(words)
    assert "beyond the edge of the page" in text
    _, start_here = _words_off_page(out / "00 START HERE.pdf")
    assert "1 message is shown as plain text because its formatting would have run text past the edge" in " ".join(start_here)
