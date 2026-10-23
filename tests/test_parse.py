import hashlib

import pytest

from pst2pdf.model import Participant
from pst2pdf import parse
from pst2pdf.parse import (
    READPST_ICS_NAME,
    clean_html,
    normalize_message_id,
    parse_eml,
    stable_readpst_ics,
)

from conftest import add_headers, make_message, write_eml


def test_basic_message(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(), path)
    model = parse_eml(path)

    assert model.subject == "Quarterly report"
    assert "alice@example.com" in model.from_addr
    assert "bob@example.com" in model.to_addrs
    assert model.date.year == 2024
    assert "<p>Hello <b>world</b></p>" in model.body_html
    # The document wrapper must be stripped so the fragment embeds cleanly.
    assert "<html" not in model.body_html.lower()
    assert "<title" not in model.body_html.lower()
    assert model.attachments == []
    assert model.kind == "mail"
    assert model.folder == ""


def test_folder_argument_is_recorded(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(), path)
    assert parse_eml(path, folder="Inbox/Projects").folder == "Inbox/Projects"


def test_plain_text_fallback(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(html=None, plain="line one\n<not a tag>"), path)
    model = parse_eml(path)

    assert model.body_html.startswith("<pre>")
    assert "&lt;not a tag&gt;" in model.body_html


def test_attachments_extracted(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(attachments=[("Q1 report.xlsx", b"SPREADSHEET")]), path)
    model = parse_eml(path)

    assert len(model.attachments) == 1
    att = model.attachments[0]
    assert att.filename == "Q1 report.xlsx"
    assert att.content == b"SPREADSHEET"
    assert att.size == len(b"SPREADSHEET")


def test_inline_image_becomes_data_uri(tmp_path):
    path = tmp_path / "1.eml"
    html = '<html><body><p>Chart:</p><img src="cid:img1" alt="chart"></body></html>'
    write_eml(make_message(html=html, inline_png_cid="img1"), path)
    model = parse_eml(path)

    assert 'src="data:image/png;base64,' in model.body_html
    assert "cid:img1" not in model.body_html
    # The hygiene pass keeps the image but drops its alt text.
    assert 'alt="' not in model.body_html
    # The fixture image is a single pixel: embedded, but too small to carry
    # anything that needs redaction.
    assert model.has_images is False
    # An inline image is part of the body, not an attachment.
    assert model.attachments == []


def test_has_images_false_without_img(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(), path)
    assert parse_eml(path).has_images is False


def test_rtf_body_flagged_not_attached(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(
        make_message(html=None, attachments=[("rtf-body.rtf", b"{\\rtf1 hi}")]), path
    )
    model = parse_eml(path)

    assert model.had_rtf_body is True
    assert model.attachments == []
    assert "<pre>" in model.body_html


def test_missing_date_and_subject(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(subject="", date=None, html=None), path)
    model = parse_eml(path)

    assert model.date is None
    assert model.subject == ""
    assert model.message_id is None
    assert model.in_reply_to is None
    assert model.references == []
    assert model.thread_index is None
    assert model.content_hash  # still filled so dedup has a key


# ---------------------------------------------------------------------------
# Threading headers
# ---------------------------------------------------------------------------


def test_threading_headers_normalized(tmp_path):
    path = tmp_path / "1.eml"
    msg = add_headers(
        make_message(),
        message_id=" <msg-3@example.com> ",
        in_reply_to="<msg-2@example.com>",
        references="<msg-1@example.com> <msg-2@example.com>",
        thread_index="AQHZ8s5qNfJk0xGp8UeZg3+4kJ2rTw==",
    )
    write_eml(msg, path)
    model = parse_eml(path)

    assert model.message_id == "msg-3@example.com"
    assert model.in_reply_to == "msg-2@example.com"
    assert model.references == ["msg-1@example.com", "msg-2@example.com"]
    assert model.thread_index == "AQHZ8s5qNfJk0xGp8UeZg3+4kJ2rTw=="


def test_folded_references_header(tmp_path):
    # Long References headers arrive folded across lines; the parser unfolds.
    path = tmp_path / "1.eml"
    raw = make_message().as_bytes()
    raw = b"References: <msg-1@example.com>\r\n <msg-2@example.com>\r\n" + raw
    path.write_bytes(raw)
    assert parse_eml(path).references == ["msg-1@example.com", "msg-2@example.com"]


def test_references_without_angle_brackets(tmp_path):
    path = tmp_path / "1.eml"
    msg = add_headers(make_message(), references="a@x b@x  a@x")
    write_eml(msg, path)
    assert parse_eml(path).references == ["a@x", "b@x"]


def test_normalize_message_id():
    assert normalize_message_id("<a@b>") == "a@b"
    assert normalize_message_id("  a@b  ") == "a@b"
    assert normalize_message_id("") is None
    assert normalize_message_id(None) is None
    assert normalize_message_id("<>") is None


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------


def test_participants_from_from_to_cc(tmp_path):
    path = tmp_path / "1.eml"
    msg = make_message()
    add_headers(
        msg,
        **{
            "From": '"Example, Alice" <Alice@Example.COM>',
            "To": "Bob Example <bob@example.com>, dave@example.com",
            "Cc": '"Carol Q. Example" <CAROL@example.com>',
        },
    )
    write_eml(msg, path)
    model = parse_eml(path)

    assert model.participants == [
        Participant(name="Example, Alice", address="alice@example.com"),
        Participant(name="Bob Example", address="bob@example.com"),
        Participant(name="", address="dave@example.com"),
        Participant(name="Carol Q. Example", address="carol@example.com"),
    ]


def test_participants_deduplicated(tmp_path):
    path = tmp_path / "1.eml"
    msg = make_message()
    add_headers(msg, To="Alice Example <alice@example.com>")
    write_eml(msg, path)
    model = parse_eml(path)
    assert model.participants == [
        Participant(name="Alice Example", address="alice@example.com"),
    ]


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def test_content_hash_follows_contract(tmp_path):
    path = tmp_path / "1.eml"
    write_eml(make_message(), path)
    model = parse_eml(path)

    body_digest = hashlib.sha256(model.body_html.encode("utf-8")).hexdigest()
    joined = "|".join(
        [model.date.isoformat(), model.from_addr, model.to_addrs, model.subject, body_digest]
    )
    assert model.content_hash == hashlib.sha256(joined.encode("utf-8")).hexdigest()


def test_content_hash_identical_for_identical_messages(tmp_path):
    a, b = tmp_path / "a.eml", tmp_path / "b.eml"
    write_eml(make_message(), a)
    write_eml(make_message(), b)
    assert parse_eml(a).content_hash == parse_eml(b).content_hash
    c = tmp_path / "c.eml"
    write_eml(make_message(subject="Other"), c)
    assert parse_eml(c).content_hash != parse_eml(a).content_hash


# ---------------------------------------------------------------------------
# Body hygiene applied during parse
# ---------------------------------------------------------------------------


def test_parse_applies_hygiene(tmp_path):
    path = tmp_path / "1.eml"
    html = (
        "<html><head><style>p{color:red}</style></head><body>"
        '<p title="tip">Read <a href="https://example.com/x">this</a> or '
        '<a href="mailto:bob@example.com">mail Bob</a>.</p>'
        "<script>alert(1)</script></body></html>"
    )
    write_eml(make_message(html=html), path)
    body = parse_eml(path).body_html

    assert "href" not in body
    assert "this [https://example.com/x]" in body
    assert "mail Bob" in body and "mailto" not in body
    assert "title=" not in body
    assert "<script" not in body and "alert" not in body
    assert "<style" not in body and "color:red" not in body


# ---------------------------------------------------------------------------
# clean_html
# ---------------------------------------------------------------------------


def test_clean_link_with_different_text_gets_bracketed_url():
    out = clean_html('<p>See <a href="https://x.example/doc">the doc</a>.</p>')
    assert out == "<p>See <a>the doc [https://x.example/doc]</a>.</p>"


def test_clean_link_text_equal_to_url_is_left_alone():
    assert clean_html('<a href="http://example.com/">http://example.com</a>') == (
        "<a>http://example.com</a>"
    )
    assert clean_html('<a href="https://www.example.com/x">example.com/x</a>') == (
        "<a>example.com/x</a>"
    )


def test_clean_mailto_and_fragment_links_dropped_silently():
    assert clean_html('<a href="mailto:bob@example.com">Bob</a>') == "<a>Bob</a>"
    assert clean_html('<a href="MAILTO:Bob@Example.com?subject=x">Bob</a>') == "<a>Bob</a>"
    assert clean_html('<a href="#top">top</a>') == "<a>top</a>"
    assert clean_html('<a href="javascript:void(0)">x</a>') == "<a>x</a>"


def test_clean_image_only_link_shows_url():
    out = clean_html('<a href="http://example.com/"><img src="data:image/png;base64,AA" alt="logo"></a>')
    assert out == '<a><img src="data:image/png;base64,AA">[http://example.com/]</a>'


def test_clean_nested_markup_inside_link():
    out = clean_html('<a href="http://a.example"><b>Bold</b> and <i>italic</i></a>')
    assert out == "<a><b>Bold</b> and <i>italic</i> [http://a.example]</a>"


def test_clean_strips_alt_title_and_event_handlers():
    out = clean_html('<p title="t" onclick="x()" class="c"><img src="data:x" alt="a" title="b" width="3"></p>')
    assert out == '<p><img src="data:x" width="3"></p>'


def test_clean_removes_dangerous_elements_and_their_content():
    html = (
        "<div><script>alert(1)</script><style>p{}</style><p>kept</p>"
        '<iframe src="x"><p>gone</p></iframe><object data="o"><p>gone too</p></object>'
        '<link rel="stylesheet" href="s.css"><meta charset="utf-8">'
        '<form action="x"><input type="text" value="secret"><b>form text</b></form></div>'
    )
    out = clean_html(html)
    assert out == "<div><p>kept</p><b>form text</b></div>"


def test_clean_drops_comments_and_conditional_comments():
    out = clean_html("<!--[if mso]><p>mso only</p><![endif]--><!-- note --><p>x</p>")
    assert out == "<p>x</p>"


def test_clean_survives_malformed_markup():
    out = clean_html('<p>broken <b>bold <i>nested</p><a href="http://a.b">unclosed')
    assert out == '<p>broken <b>bold <i>nested</p><a>unclosed [http://a.b]</a>'
    # Unclosed script must not swallow the rest, since there is no rest.
    assert clean_html("<p>a</p><script>never closed") == "<p>a</p>"
    # Garbage that is not HTML at all round-trips as text.
    assert clean_html("just < text & stuff >") == "just &lt; text &amp; stuff &gt;"


def test_clean_reescapes_text_and_attributes():
    out = clean_html('<p data-x="a &amp; b">Tom &amp; Jerry &lt; 3 &copy;</p>')
    assert out == '<p data-x="a &amp; b">Tom &amp; Jerry &lt; 3 ©</p>'


def test_clean_void_elements_and_self_closing():
    assert clean_html("<p>a<br/>b<br>c<hr></p>") == "<p>a<br>b<br>c<hr></p>"


def test_clean_hidden_style_left_alone():
    out = clean_html('<div style="display:none">hidden</div>')
    assert out == '<div style="display:none">hidden</div>'


# ---------------------------------------------------------------------------
# Hidden-text channels in inline styles and attributes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declaration",
    [
        "position: absolute",
        "position:fixed",
        "left: -5000px",
        "right: -9999px",
        "top: -5000px",
        "bottom: -100em",
        "opacity: 0",
        "text-indent: -9999px",
        "clip: rect(0 0 0 0)",
        "clip-path: inset(100%)",
        "bookmark-level: 1",
        "bookmark-label: 'Jane Doe'",
        "bookmark-state: closed",
        "string-set: heading content()",
        "-weasy-anything: 1",
        "-WEASY-hyphens: auto",
        "POSITION: ABSOLUTE",
    ],
)
def test_clean_drops_hiding_style_declarations(declaration):
    out = clean_html(f'<span style="color: white; {declaration}; font-size: 1px">Jane Doe</span>')
    assert out == '<span style="color: white; font-size: 1px">Jane Doe</span>'
    # The declaration alone leaves no style attribute behind.
    assert clean_html(f'<span style="{declaration}">x</span>') == "<span>x</span>"


def test_clean_keeps_other_style_declarations_verbatim():
    style = "color:#fff;background:url(data:image/png;base64,AAA;BBB);font-family:'a;b',serif;display:none"
    out = clean_html(f'<p style="{style}">x</p>')
    # Declarations are re-joined with "; "; the ";" inside url() and quotes is
    # not a separator. (Quotes are entity-escaped in attributes, as always.)
    assert out == (
        "<p style=\"color:#fff; background:url(data:image/png;base64,AAA;BBB); "
        "font-family:&#x27;a;b&#x27;,serif; display:none\">x</p>"
    )


def test_clean_drops_id_name_and_class_attributes():
    # A class can only match pst2pdf's own template rules (.addr, .nowrap),
    # which would stop the text from wrapping.
    out = clean_html('<a id="top" name="top" class="nowrap">t</a><p id="p1" data-id="keep">x</p>')
    assert out == '<a>t</a><p data-id="keep">x</p>'


def test_clean_makes_every_white_space_wrap():
    out = clean_html(
        '<td nowrap style="white-space: nowrap !important; color:red">a</td>'
        '<div style="WHITE-SPACE:pre">b</div><p style="white-space: pre-line">c</p>'
    )
    assert out == (
        '<td style="white-space: normal; color:red">a</td>'
        '<div style="white-space: pre-wrap">b</div><p style="white-space: pre-line">c</p>'
    )


def test_clean_drops_declarations_that_stop_wrapping():
    out = clean_html('<a style="min-width: 900px; overflow-wrap: normal; word-wrap: normal; word-break: keep-all; color: blue">x</a>')
    assert out == '<a style="color: blue">x</a>'


def test_clean_drops_fixed_widths_but_not_relative_ones():
    out = clean_html(
        '<table width="600" style="width:6.25in"><colgroup width="300"><col width="200"></colgroup>'
        '<tr><td width="600" style="width:6.25in; padding:0">a</td><th width="100%">b</th></tr></table>'
        '<table width="100%" style="width: 100% !important"><tr><td>c</td></tr></table>'
        '<div style="width: 900PX; float: right">d</div><div style="width:150%">e</div>'
        '<div style="width: auto; width: calc(100% - 4px)">f</div>'
        '<img src="data:x" width="800" style="width: 800px">'
    )
    assert out == (
        "<table><colgroup><col></colgroup>"
        '<tr><td style="padding:0">a</td><th>b</th></tr></table>'
        '<table width="100%" style="width: 100% !important"><tr><td>c</td></tr></table>'
        '<div style="float: right">d</div><div>e</div>'
        '<div style="width: auto; width: calc(100% - 4px)">f</div>'
        '<img src="data:x" width="800" style="width: 800px">'
    )


def test_clean_drops_negative_margins_transforms_and_clipping():
    out = clean_html(
        '<p style="margin-left: -200px; margin: 0 0 -.5em 0; margin-top: 4px; padding-left: -3px">a</p>'
        '<div style="transform: translateX(-9999px); overflow: hidden; overflow-x: clip; height: 0">b</div>'
    )
    assert out == '<p style="margin-top: 4px; padding-left: -3px">a</p><div style="height: 0">b</div>'


def test_clean_wraps_long_unbroken_runs_so_they_can_break():
    run = "x" * parse.LONG_RUN
    out = clean_html(f"<p>short words and {run} and {run[:-1]}</p>")
    assert out == f'<p>short words and <span style="overflow-wrap: anywhere">{run}</span> and {run[:-1]}</p>'
    # A non-breaking space does not break a run; escaping happens inside the span.
    out = clean_html("<p>Report&nbsp;Suspicious&nbsp;&lt;now&gt;&nbsp;please&nbsp;now</p>")
    assert out == '<p><span style="overflow-wrap: anywhere">Report\xa0Suspicious\xa0&lt;now&gt;\xa0please\xa0now</span></p>'
    # A textarea shows its content as raw text, so no markup goes in.
    assert clean_html(f"<textarea>{run}</textarea>") == f"<textarea>{run}</textarea>"


def test_clean_link_target_is_breakable():
    url = "https://urldefense.com/v3/__https://example.com/a/very/long/path__;!!abc"
    out = clean_html(f'<a href="{url}">Click</a>')
    assert out == f'<a>Click <span style="overflow-wrap: anywhere">[{url}]</span></a>'


def test_parse_applies_hidden_text_hygiene(tmp_path):
    html = (
        '<html><body><p id="intro">Visible.</p>'
        '<div style="position:absolute;left:-5000px;color:white">Jane Doe</div>'
        '<span style="opacity:0;text-indent:-9999px">SSN 123-45-6789</span></body></html>'
    )
    path = tmp_path / "1.eml"
    write_eml(make_message(html=html), path)
    body = parse_eml(path).body_html
    assert 'id="' not in body
    for prop in ("position", "left:", "opacity", "text-indent"):
        assert prop not in body
    # Text survives (it will be visible, so Search & Redact can find it); only the hiding goes.
    assert "Jane Doe" in body and 'style="color:white"' in body


def test_clean_cid_and_data_hrefs_dropped_silently():
    assert clean_html('<a href="cid:part1">img</a>') == "<a>img</a>"


def test_clean_empty_input():
    assert clean_html("") == ""


def test_visible_inline_image_sets_has_images(tmp_path):
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (200, 150), (200, 30, 30)).save(buf, "PNG")
    msg = make_message(html='<html><body><p>see</p><img src="cid:big"></body></html>')
    msg.get_body(preferencelist=("html",)).add_related(buf.getvalue(), maintype="image", subtype="png", cid="<big>")
    path = tmp_path / "big.eml"
    write_eml(msg, path)
    model = parse_eml(path)
    assert model.has_images is True
    assert "data:image/png;base64," in model.body_html


def test_large_inline_image_is_downscaled_to_jpeg(tmp_path):
    import base64
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.effect_noise((3000, 2400), 50).convert("RGB").save(buf, "PNG")
    msg = make_message(html='<html><body><img src="cid:photo"></body></html>')
    msg.get_body(preferencelist=("html",)).add_related(buf.getvalue(), maintype="image", subtype="png", cid="<photo>")
    path = tmp_path / "photo.eml"
    write_eml(msg, path)
    model = parse_eml(path)
    assert "data:image/jpeg;base64," in model.body_html
    b64 = model.body_html.split("data:image/jpeg;base64,", 1)[1].split('"', 1)[0]
    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        assert max(im.size) == 2200


# ---------------------------------------------------------------------------
# readpst-synthesized meeting .ics: same name and bytes on every extraction
# ---------------------------------------------------------------------------


def _readpst_ics(dtstamp: str, created: str | None = "20251222T170244Z") -> bytes:
    """The attachment readpst writes for a meeting message, as it writes it."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:LibPST v0.6.76",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        'ORGANIZER;CN="Alice Example":MAILTO:alice@example.com',
        "UID:0x5b0",
        f"DTSTAMP:{dtstamp}",
        *([f"CREATED:{created}"] if created else []),
        "LAST-MOD:20260610T225149Z",
        "SUMMARY:Budget meeting",
        "DTSTART;VALUE=DATE-TIME:20251231T144500Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _meeting_eml(tmp_path, name: str, filename: str, ics: bytes, date=None):
    msg = make_message(date=date or "Mon, 22 Dec 2025 11:02:45 -0600")
    msg.add_attachment(ics, maintype="text", subtype="calendar", filename=filename)
    path = tmp_path / name
    write_eml(msg, path)
    return parse_eml(path).attachments[0]


def test_readpst_ics_same_sha256_across_extractions(tmp_path):
    # Two extractions of one archive: readpst picks a new rand() name and
    # stamps the extraction time, and nothing else differs.
    first = _meeting_eml(
        tmp_path, "1.eml", "i1979958226.ics", _readpst_ics("20260924T171724Z")
    )
    second = _meeting_eml(
        tmp_path, "2.eml", "i771490495.ics", _readpst_ics("20260924T171726Z")
    )

    assert first.sha256 == second.sha256
    assert first.content == second.content
    assert first.filename == second.filename == READPST_ICS_NAME
    # The sha256 is of the bytes held, so a native written from them matches it.
    assert first.sha256 == hashlib.sha256(first.content).hexdigest()
    # DTSTAMP is the message's Date in UTC; everything else is readpst's.
    assert b"DTSTAMP:20251222T170245Z\n" in first.content
    assert first.content == _readpst_ics("20251222T170245Z")


def test_readpst_ics_without_date_uses_created():
    a = stable_readpst_ics("i1.ics", _readpst_ics("20260924T171724Z"), None)
    b = stable_readpst_ics("i2.ics", _readpst_ics("20260924T171726Z"), None)
    assert a == b == (READPST_ICS_NAME, _readpst_ics("20251222T170244Z"))


def test_readpst_ics_without_date_or_created_drops_dtstamp():
    name, content = stable_readpst_ics(
        "i1.ics", _readpst_ics("20260924T171724Z", created=None), None
    )
    assert name == READPST_ICS_NAME
    assert b"DTSTAMP" not in content
    assert content == _readpst_ics("X", created=None).replace(b"DTSTAMP:X\n", b"")


def test_readpst_ics_crlf_line_endings():
    crlf = _readpst_ics("20260924T171724Z").replace(b"\n", b"\r\n")
    _, content = stable_readpst_ics("i1.ics", crlf, None)
    assert b"DTSTAMP:20251222T170244Z\r\nCREATED:" in content


@pytest.mark.parametrize(
    "filename, content",
    [
        # A sender's invitation: not readpst's PRODID, even under readpst's name pattern.
        (
            "i12345.ics",
            _readpst_ics("20260924T171724Z").replace(
                b"LibPST v0.6.76", b"-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN"
            ),
        ),
        # readpst's PRODID under a sender's own file name.
        ("invite.ics", _readpst_ics("20260924T171724Z")),
        ("I12345.ics", _readpst_ics("20260924T171724Z")),
        ("i12345.ics.txt", _readpst_ics("20260924T171724Z")),
    ],
)
def test_sender_ics_left_untouched(tmp_path, filename, content):
    att = _meeting_eml(tmp_path, "1.eml", filename, content)
    assert att.filename == filename
    assert att.content == content
