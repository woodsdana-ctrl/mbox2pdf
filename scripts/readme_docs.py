"""Render README.md as docs/README.html and docs/README.pdf.

The HTML is one self-contained file: the stylesheet is inline and nothing is
fetched, so it can be emailed or opened offline. Links to other files in the
repository (PRIVACY.md, docs/DESIGN.md) point at GitHub, since a relative
link breaks as soon as the file leaves the checkout. The PDF is rendered from
that HTML by WeasyPrint as a tagged PDF/UA-1 file, and carries the SHA-256 of
the HTML it came from in its document information (``/SourceSHA256``), which
is how this script and tests/test_docs_sync.py tell a stale PDF from a
current one without WeasyPrint.

    .venv/bin/python scripts/readme_docs.py          # regenerate what is stale
    .venv/bin/python scripts/readme_docs.py --check  # exit 1 if either is stale

WeasyPrint does not run on the host (no Pango), so when it cannot be imported
the PDF is rendered inside the pst2pdf image, with no network, from the HTML
just written. Only the HTML step needs markdown-it-py (the dev extra).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
HTML_OUT = ROOT / "docs" / "README.html"
PDF_OUT = ROOT / "docs" / "README.pdf"

REPO_URL = "https://github.com/woodsdana-ctrl/mbox2pdf/blob/main/"
IMAGE = "pst2pdf"
SOURCE_KEY = "SourceSHA256"

STYLE = """\
:root {
  color-scheme: light dark;
  --fg: #1f2328; --muted: #59636e; --bg: #ffffff;
  --code-bg: #f6f8fa; --border: #d1d9e0; --link: #0550ae;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #e6edf3; --muted: #9198a1; --bg: #0d1117;
    --code-bg: #161b22; --border: #3d444d; --link: #4493f8;
  }
}
html { background: var(--bg); }
body {
  margin: 0 auto; max-width: 52rem; padding: 2rem 1rem 4rem;
  color: var(--fg); background: var(--bg);
  font: 1rem/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "DejaVu Sans", Helvetica, Arial, sans-serif;
}
h1, h2, h3 { line-height: 1.25; margin: 1.6em 0 0.6em; }
h1 { font-size: 2rem; margin-top: 0; }
h1, h2 { padding-bottom: 0.3em; border-bottom: 1px solid var(--border); }
h2 { font-size: 1.5rem; }
h3 { font-size: 1.2rem; }
a { color: var(--link); }
code, pre { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace; }
code { font-size: 0.875em; padding: 0.1em 0.3em; border-radius: 4px; background: var(--code-bg); }
pre {
  overflow-x: auto; padding: 0.8rem 1rem; line-height: 1.45;
  border: 1px solid var(--border); border-radius: 6px; background: var(--code-bg);
}
pre code { padding: 0; font-size: 0.8125rem; background: none; }
li + li { margin-top: 0.25em; }
table { border-collapse: collapse; margin: 1rem 0; }
th, td { padding: 0.35em 0.75em; border: 1px solid var(--border); text-align: left; }
.source { margin-top: 3rem; font-size: 0.875rem; color: var(--muted); }
.callout { margin: 1rem 0; padding: 0.5rem 1rem; border-left: 0.25em solid var(--callout); }
.callout > p { margin: 0.5em 0; }
.callout-title { display: block; color: var(--callout); }
:root { --callout: #8250df; }
@media (prefers-color-scheme: dark) { :root { --callout: #ab7df8; } }

@page {
  size: letter; margin: 0.75in 0.7in;
  @bottom-center { content: "pst2pdf · page " counter(page) " of " counter(pages);
                   font: 8pt "DejaVu Sans", sans-serif; color: #59636e; }
}
@media print {
  body { max-width: none; padding: 0; font: 10pt/1.5 "DejaVu Sans", sans-serif; }
  code, pre { font-family: "DejaVu Sans Mono", monospace; }
  h1 { font-size: 20pt; } h2 { font-size: 14pt; } h3 { font-size: 11.5pt; }
  h2, h3 { break-after: avoid; }
  pre { overflow: visible; white-space: pre-wrap; overflow-wrap: anywhere; padding: 6pt 8pt; orphans: 4; widows: 4; }
  pre code { font-size: 7.5pt; }
}
"""

TEMPLATE = """\
<!DOCTYPE html>
<!-- Generated from README.md by scripts/readme_docs.py. Edit README.md, not this file. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{style}</style>
</head>
<body>
<main>
{body}</main>
<footer class="source">
<p>Generated from README.md in the <a href="{repo}">pst2pdf repository</a>.</p>
</footer>
</body>
</html>
"""


def _slug(text: str, seen: dict[str, int]) -> str:
    """GitHub's heading anchors: lowercase, punctuation dropped, spaces to hyphens."""
    slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
    n = seen.get(slug, 0)
    seen[slug] = n + 1
    return f"{slug}-{n}" if n else slug


#: GitHub's alert blockquotes ("> [!IMPORTANT]" on the first line), which
#: CommonMark renders as a quote beginning with the literal marker.
ALERTS = {"NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important", "WARNING": "Warning", "CAUTION": "Caution"}


def _alerts(tokens) -> None:
    """Turn GitHub alert blockquotes into a titled callout, as GitHub shows them."""
    from markdown_it.token import Token

    for i, token in enumerate(tokens):
        if token.type != "blockquote_open" or i + 2 >= len(tokens) or tokens[i + 1].type != "paragraph_open":
            continue
        children = tokens[i + 2].children or []
        m = re.fullmatch(r"\[!([A-Z]+)\]", children[0].content.strip()) if children and children[0].type == "text" else None
        if not m or m.group(1) not in ALERTS:
            continue
        rest = children[1:]  # keeps the line break, so the title never runs into the first word
        title = [Token("strong_open", "strong", 1), Token("text", "", 0), Token("strong_close", "strong", -1)]
        title[0].attrSet("class", "callout-title")
        title[1].content = ALERTS[m.group(1)]
        tokens[i + 2].children = title + rest
        token.tag = "div"
        token.attrSet("class", f"callout {m.group(1).lower()}")
        token.attrSet("role", "note")
        depth = 0
        for close in tokens[i:]:
            depth += close.nesting if close.type.startswith("blockquote_") else 0
            if close.type == "blockquote_close" and depth == 0:
                close.tag = "div"
                break


def render_html(markdown_text: str) -> str:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(markdown_text)
    _alerts(tokens)
    seen: dict[str, int] = {}
    title = "pst2pdf"
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            text = "".join(c.content for c in tokens[i + 1].children or [] if c.type in ("text", "code_inline"))
            token.attrSet("id", _slug(text, seen))
            if token.tag == "h1":
                title = text
        elif token.type == "th_open":
            token.attrSet("scope", "col")
        elif token.type == "inline":
            for child in token.children or []:
                href = child.attrGet("href") if child.type == "link_open" else None
                if href and not re.match(r"[a-z][a-z0-9+.-]*:|#", href):
                    child.attrSet("href", REPO_URL + href)
    body = md.renderer.render(tokens, md.options, {})
    # A code block wider than the page scrolls; tabindex lets a keyboard reach it.
    body = body.replace("<pre>", '<pre tabindex="0">')
    return TEMPLATE.format(title=html.escape(title), style=STYLE, body=body, repo=REPO_URL.split("/blob/")[0])


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pdf_source_sha256(path: Path) -> str | None:
    """The HTML SHA-256 recorded in a rendered PDF, or None if there is none."""
    if not path.exists():
        return None
    from pypdf import PdfReader

    value = (PdfReader(path).metadata or {}).get(f"/{SOURCE_KEY}")
    return str(value) if value else None


def render_pdf(html_text: str, path: Path) -> None:
    from weasyprint import HTML

    document = HTML(string=html_text, base_url=str(path.parent), media_type="print").render()
    document.metadata.custom = {SOURCE_KEY: sha256(html_text)}  # not the viewport <meta>
    document.write_pdf(path, pdf_variant="pdf/ua-1", custom_metadata=True)


def _weasyprint_available() -> bool:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import weasyprint  # noqa: F401
    except (ImportError, OSError):  # OSError: the package is there but Pango is not
        return False
    return True


def _docker() -> str:
    for candidate in ("docker", "/usr/local/bin/docker", "/opt/homebrew/bin/docker", "~/.orbstack/bin/docker"):
        found = shutil.which(os.path.expanduser(candidate))
        if found:
            return found
    sys.exit("readme_docs: WeasyPrint is not importable here and no docker CLI was found")


def render_pdf_in_docker() -> None:
    """Render the PDF with the pst2pdf image's WeasyPrint and fonts. Only
    scripts/ (read-only) and docs/ are mounted, and there is no network."""
    docker = _docker()
    if subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True).returncode:
        sys.exit(f"readme_docs: no {IMAGE} image; build it with `docker build -t {IMAGE} .` and retry")
    subprocess.run(
        [docker, "run", "--rm", "--network", "none",
         "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
         "-v", f"{ROOT / 'scripts'}:/src/scripts:ro", "-v", f"{ROOT / 'docs'}:/src/docs",
         "--entrypoint", "/usr/bin/tini", IMAGE, "--",
         "python", "/src/scripts/readme_docs.py", "--pdf-only"],
        check=True,
    )  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report stale files and exit 1 instead of writing")
    parser.add_argument("--pdf-only", action="store_true", help=argparse.SUPPRESS)  # inside the container
    args = parser.parse_args(argv)

    if args.pdf_only:
        html_text = HTML_OUT.read_text(encoding="utf-8")
        render_pdf(html_text, PDF_OUT)
        return 0

    html_text = render_html(README.read_text(encoding="utf-8"))
    html_stale = not HTML_OUT.exists() or HTML_OUT.read_text(encoding="utf-8") != html_text
    pdf_stale = pdf_source_sha256(PDF_OUT) != sha256(html_text)
    if args.check:
        for stale, path in ((html_stale, HTML_OUT), (pdf_stale, PDF_OUT)):
            if stale:
                print(f"{path.relative_to(ROOT)} is out of date with README.md")
        return 1 if html_stale or pdf_stale else 0

    if html_stale:
        HTML_OUT.write_text(html_text, encoding="utf-8")
        print(f"wrote {HTML_OUT.relative_to(ROOT)}")
    if pdf_stale:
        if _weasyprint_available():
            render_pdf(html_text, PDF_OUT)
        else:
            render_pdf_in_docker()
        print(f"wrote {PDF_OUT.relative_to(ROOT)}")
    if not (html_stale or pdf_stale):
        print("docs/README.html and docs/README.pdf are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
