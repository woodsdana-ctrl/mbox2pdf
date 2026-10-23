"""README.md, the wrapper's --help and the CLI's options must agree.

These tests fail as soon as a flag is added to, renamed in, or removed from one place and not the
others, so the drift is caught in the commit that causes it. The same goes for
docs/README.html and docs/README.pdf, which are generated from README.md.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from pst2pdf import cli

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
WRAPPER = (ROOT / "pst2pdf.sh").read_text(encoding="utf-8")

#: A flag at the start of a line or after a space, "(", "," or a backtick,
#: so that "non-empty", "Bates-numbered" and "<archive-name>-pdfs" are not flags.
FLAG = re.compile(r"(?:^|(?<=[\s(,`]))(--?[a-zA-Z][\w-]*)", re.MULTILINE)

#: Flags that only the wrapper handles: it never forwards them to the container.
WRAPPER_ONLY = {"-o", "--output", "-f", "--force", "--rebuild", "--zip", "-h", "--help"}


#: The flags an option list defines: those that open an entry line
#: ("-o, --output DIR"), indented at most 8 spaces. A flag inside a
#: description does not count, even where a wrapped description line begins
#: with one (continuation lines are indented 20 or more).
LISTED = re.compile(r"^ {0,8}(--?[a-zA-Z][\w-]*(?:, *--?[a-zA-Z][\w-]*)*)", re.MULTILINE)


def _flags(text: str) -> set[str]:
    return set(FLAG.findall(text))


def _listed(text: str) -> set[str]:
    return {flag.strip() for entry in LISTED.findall(text) for flag in entry.split(",")}


def _wrapper_flags() -> tuple[set[str], set[str]]:
    """(accepted, removed): the top-level case arms of the wrapper's argument
    loop. An arm whose body is only a ``die`` is a removed flag kept to say so."""
    loop = WRAPPER[WRAPPER.index('while [ $# -gt 0 ]; do') : WRAPPER.index("    done\n")]
    lines = loop.splitlines()
    accepted: set[str] = set()
    removed: set[str] = set()
    for i, line in enumerate(lines):
        m = re.match(r"^ {12}(-[^)\s]*)\)(.*)$", line)
        if not m or m.group(1) == "-*":
            continue
        body = m.group(2).strip() or lines[i + 1].strip()
        (removed if body.startswith("die ") else accepted).update(m.group(1).split("|"))
    return accepted, removed


def _wrapper_usage() -> str:
    start = WRAPPER.index("usage() {")
    return WRAPPER[WRAPPER.index("<<EOF\n", start) : WRAPPER.index("\nEOF\n", start)]


def _readme_options() -> str:
    """The option list: the code block that follows "Options:" under Usage."""
    start = README.index("input file. Options:")
    begin = README.index("```\n", start) + 4
    return README[begin : README.index("```", begin)]


def _cli_flags() -> set[str]:
    return {
        opt
        for action in cli.build_parser()._actions
        if action.help != argparse.SUPPRESS
        for opt in action.option_strings
    }


def test_the_parsers_find_what_they_should():
    """Guards the guards: an empty set would make every test below pass."""
    accepted, removed = _wrapper_flags()
    assert {"--triage", "--zip", "-o", "--appendix-over", "--no-bates"} <= accepted
    assert removed == {"--separate"}
    assert "--decisions" in _listed(_readme_options()) and "-o" in _listed(_wrapper_usage())
    assert "--overlay" in _cli_flags() and "--separate" not in _cli_flags()


def test_readme_lists_every_wrapper_option():
    accepted, _ = _wrapper_flags()
    missing = accepted - _listed(_readme_options())
    assert not missing, f"README.md's option list is missing {sorted(missing)}"


def test_readme_lists_no_option_the_wrapper_refuses():
    accepted, _ = _wrapper_flags()
    extra = _listed(_readme_options()) - accepted
    assert not extra, f"README.md's option list names {sorted(extra)}, which pst2pdf.sh does not accept"


def test_wrapper_help_lists_exactly_what_it_accepts():
    accepted, _ = _wrapper_flags()
    usage = _listed(_wrapper_usage())
    assert not accepted - usage, f"pst2pdf.sh --help is missing {sorted(accepted - usage)}"
    assert not usage - accepted, f"pst2pdf.sh --help names {sorted(usage - accepted)}, which it does not accept"


def test_wrapper_forwards_only_flags_the_cli_knows():
    accepted, _ = _wrapper_flags()
    unknown = accepted - WRAPPER_ONLY - _cli_flags()
    assert not unknown, f"pst2pdf.sh forwards {sorted(unknown)}, which the CLI does not accept"


def test_readme_names_every_cli_option():
    """Options the wrapper forwards are in the option list (checked above);
    the rest must at least be named, under "use Docker directly"."""
    missing = _cli_flags() - _flags(README)
    assert not missing, f"README.md never names the CLI option(s) {sorted(missing)}"


def _readme_docs():
    """scripts/readme_docs.py, which is not part of the package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("readme_docs", ROOT / "scripts" / "readme_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGENERATE = "run .venv/bin/python scripts/readme_docs.py and commit the result"


def test_readme_html_matches_readme():
    pytest.importorskip("markdown_it")
    docs = _readme_docs()
    current = docs.HTML_OUT.read_text(encoding="utf-8")
    assert current == docs.render_html(README), f"docs/README.html is out of date with README.md: {REGENERATE}"


def test_readme_pdf_was_rendered_from_the_current_html():
    """The PDF records the SHA-256 of the HTML it was rendered from, so this
    needs neither WeasyPrint nor markdown-it-py."""
    docs = _readme_docs()
    html_sha = docs.sha256(docs.HTML_OUT.read_text(encoding="utf-8"))
    assert docs.pdf_source_sha256(docs.PDF_OUT) == html_sha, f"docs/README.pdf is out of date: {REGENERATE}"


def test_github_alerts_become_titled_callouts():
    """"> [!IMPORTANT]" is a callout on GitHub; the HTML and PDF must not show
    the marker as text, and an ordinary quote must stay a quote."""
    pytest.importorskip("markdown_it")
    docs = _readme_docs()
    out = docs.render_html("# T\n\n> [!IMPORTANT]\n> Rebuild after updating.\n\n> Just a quote.\n")
    body = out[out.index("<main>") : out.index("</main>")]
    assert "[!IMPORTANT]" not in body
    assert '<div class="callout important" role="note">\n<p><strong class="callout-title">Important</strong>\nRebuild after updating.</p>\n</div>' in body
    assert "<blockquote>\n<p>Just a quote.</p>\n</blockquote>" in body

