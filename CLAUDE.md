# Browser review additions

Read docs/REVIEW.md for the staff workflow and docs/DESIGN.md for the selection
boundary. review.py owns SQLite decisions and the offline worker; review_server.py
and web/ own inert-text previews and local HTTP. Keep generation in a separate
network_mode:none container. Do not render email HTML in the UI or mount the
Docker socket. Filter only through prepared_models before all PDF stages; never
put excluded content, reasons or source metadata into volume ZIPs. Review IDs are
strict content hashes bound to source archive manifests, not rows or Message-ID.
Run tests/test_review.py and the optional real Chromium test_review_browser.py.

# MBOX adaptation

This checkout reuses upstream pst2pdf; read the Gmail quick start in README.md
and the MBOX intake contract in docs/DESIGN.md first. Both CLI entry points share
one pipeline. MBOX intake lives in mbox.py and shares parse_message with EML.
MBOX uses strict MIME fingerprints and reference-only threading; preserve PST's
legacy behavior. Keep LICENSE, NOTICE and upstream attribution. Use synthetic
fixtures only. Regenerate docs/README.html and docs/README.pdf after README edits.
MBOX attachment payloads use DiskAttachment spools; do not retain all bytes in
RAM. Bodies and models still scale with export size. Original exports are read-only.

# pst2pdf

Converts an Outlook .pst archive into PDF review volumes, Bates-numbered by
default, ready for redaction in Adobe Acrobat. Built for responding to a
records or discovery request: an operator exports the search results as a
.pst (from Microsoft Purview, say), this tool converts it, and reviewers
redact the result in Acrobat. The reviewers are not necessarily technical and
their time is the expensive part, so every output decision is judged by
whether it shortens their redaction pass and whether an unredacted copy of a
name could survive outside visible page content. Runs in a Docker container.
Python throughout.

## Status

v0.3 made Bates stamping optional (`--no-bates`), removed attachment cover
sheets, and took the message number out of the footer. After v0.3, for the
same reason (a number on a released page tells the requester how much was
collected), the message header lost its number, folders and copy count, and the one-page
notes and appendix sheets name a message by date and subject, with no item
number or count of messages or copies. Only a triage copy keeps the header
line (`cli._header_origin`), and the index, the exceptions table, bookmarks
and the CSVs keep numbers.

v0.2 replaced the per-message PDF tree with review volumes.
See “Why review volumes” in README.md for the reasoning and `docs/DESIGN.md`
for the module contract the implementation follows. The legacy layout
(`--separate`) was removed and replaced by a triage run (`--triage`) and an
`omit` decision. Unit tests run on the host with `.venv/bin/python -m pytest`
and skip what needs WeasyPrint, LibreOffice, or readpst. The Docker image is
the integration environment and runs the whole suite. Pre-marked redactions
have not yet been confirmed in Acrobat, and `docs/PREMARK-VERIFICATION.md` is the
checklist for that.

### Docker

Any Docker-compatible runtime works. Non-login shells (like tool
invocations) may not have the `docker` CLI on PATH; if `docker` is not found,
find its full path from a login shell (`command -v docker`) and call that.

Build and run:

```
docker build -t pst2pdf .
docker run --rm --network none -v "$(pwd)":/data pst2pdf /data/archive.pst /data/output
```

`--network none` is correct for real runs, because rendering blocks all
non-`data:` resource fetches, so it needs no network. Only drop the flag if running the test
suite inside the container, since `pip install pytest` needs to reach the index.

Something that reaps orphans has to be PID 1. The image’s ENTRYPOINT is
`tini -- pst2pdf` for that reason. LibreOffice probes for a signing backend
whenever it opens an Office document, forking gpgconf/gpg/gpgsm that exit
before it does, and soffice.bin outlives the oosplash launcher `convert.py`
waits on. All of them are reparented to PID 1, and Python reaps only its own
children, so as PID 1 it accumulated five zombies per Office attachment until
the wrapper’s `--pids-limit 512` was exhausted. The next thread WeasyPrint
asked Pango for then failed with EAGAIN, which GLib turns into a fatal error, and a
PID 1 with no SIGABRT handler ignores the abort and hangs the run instead of
ending it. Override the entrypoint to run the test suite
(`--entrypoint /usr/bin/tini pst2pdf -- python -m pytest`), not to run a
conversion. The Dockerfile’s `pst2pdf.image-revision` label (1: tini, 2: the
CJK font, 3: the emoji font, 4: a font cache that stays valid, 5: stable
readpst meeting .ics attachments, 6: `--triage`, 7: `--withheld-by` and the
`DOC-` prefix, 8: `--search-report`) is compared with the image’s, and the wrapper rebuilds any older
image. Bump it whenever an existing image must not be reused. The wrapper
also labels every image it builds with `pst2pdf.source`, a fingerprint of
`src/` (bytecode aside), the Dockerfile and `pyproject.toml`
(`source_fingerprint` in the wrapper), and rebuilds whenever the checkout’s
differs or the label is missing (an image built by hand), so a `git pull`
never runs old code in an old image. That covers every code change, so the
revision is now only a fallback for a host with neither `sha256sum` nor
`shasum`.

## Architecture

The user-facing front door is `pst2pdf.sh`, a bash wrapper that locates a
docker CLI (PATH, then common install locations), builds the image on
first use, refuses non-empty output directories without `--force`, folds the
container’s per-message log lines into a live progress counter and shows
`INFO stage:` lines as steps, mounts a `--terms` file read-only, and can zip
the output. It always passes `-v` to the container and does the filtering
itself. With `--survey` it says “Surveying” and ends by pointing at
`Attachment survey.pdf`. With `--triage` it ends by pointing at the attachment
index. `--zip` never packs the unredacted originals folder of a production
run. A triage run is kept back whole, so its zip is named `… - triage - DO NOT
RELEASE.zip` and includes them.

Inside the container, `cli.build_volumes` orchestrates these modules, each
communicating only through the dataclasses in `model.py`:

1. **extract.py** — `readpst -j 0 -e -8`. `-j 0` is load-bearing: it is
   readpst’s only serial mode. Any `-j N` with N ≥ 1, `-j 1` included, still
   forks a child per folder, and the children race on output numbering and
   silently drop messages.
2. **parse.py / calendar.py** — `.eml` and `.ics` into `MessageModel`. Parse
   also applies the body hygiene: hrefs become visible text, alt/title
   attributes and script/style/iframe/form elements are removed, so nothing
   can reach the PDF outside page content. It also makes everything wrap to
   the page (nowrap, fixed and cell widths, negative margins and overflow
   go; runs of 30+ characters without a space get a span that may break
   anywhere), since text past the margin is clipped from view but stays in
   the text layer. `has_images` counts embedded
   images only. readpst writes a meeting message’s own appointment as an
   attachment named `i<rand()>.ics` with `DTSTAMP` set to the extraction
   time, so name and sha256 changed on every extraction and a triage
   decision matched nothing in the production run. `stable_readpst_ics`
   renames it `Calendar item.ics` and sets `DTSTAMP` to the message’s Date
   (else the file’s `CREATED`), recognizing it by the name pattern and
   `PRODID:LibPST` together, so a sender’s .ics is never touched. It
   rewrites the bytes rather than hashing around `DTSTAMP`, so every
   sha256 in the index, survey and manifest is the sha256 of the native
   file, and two extractions of one archive give identical attachments.
3. **organize.py** — dedup by Message-ID (content hash fallback), threading
   by References/In-Reply-To, Outlook Thread-Index, and subject plus shared
   participant, then ordering (`thread`, `chrono`, `folder`).
4. **render.py + templates/** — Jinja2 + WeasyPrint. `_base.html` forces one
   font (DejaVu Sans, with Noto Sans CJK only for the Chinese, Japanese and
   Korean it has no glyphs for, which were empty boxes), no ligatures, no
   hyphenation, and no outline from headings, and no template has a
   `<title>`. Emoji come from Noto Color Emoji by fontconfig fallback, drawn
   as small images over an outline-free glyph that keeps them in the text
   layer. Never name that font in CSS: it has digits and would draw dates
   and Bates numbers as emoji. Message rendering runs in a `ProcessPoolExecutor`
   as before. `render_pdf` patches WeasyPrint’s min-content measurement so
   `overflow-wrap: anywhere` counts for every text box (on 70 only a box at a
   line start could break, so tables grew past the page), then counts text
   and image boxes outside the page: a message page with any is re-rendered
   as plain text (`offpage_fallbacks`), any other page is logged and counted
   (`offpage_text`, should be 0). Text outside the page can still come from
   attachments pst2pdf does not lay out: pass-through PDFs, or a .docx
   watermark LibreOffice draws above the page.
5. **convert.py** — attachments to PDF, cached by SHA-256. An Office
   extension is trusted only when the bytes match (OLE2 for `.xls`/`.doc`,
   a zip for `.xlsx`/`.docx`): systems export tab-separated text and HTML
   tables as `.xls`, and LibreOffice printed those rows with every field run
   together, unreadable and invisible to whole-word search. Those go to the
   text or HTML path instead. LibreOffice
   headless for Office formats (fresh profile per call, macros disabled,
   timeout kill), Pillow for images, pypdf sanitizing pass-through for PDFs,
   reportlab for plain text (`textpdf.py`: the old WeasyPrint `<pre>` page
   line for line, falling back to WeasyPrint when DejaVu Sans Mono lacks a
   glyph) and for tab- or comma-delimited exports as aligned tables (header
   repeated per page, empty columns noted and left out, words never split
   across lines, landscape or 7–7.5 pt only when that saves a tenth of the
   pages), WeasyPrint for HTML and for `.eml`/`.msg`/`.ics` rendered through
   the message template, and placeholders with reasons for the rest. Text,
   HTML and nested-message layout runs in a pool of worker processes
   (forkserver, since the converter is multi-threaded by then), not behind
   one lock: on 12 logs, 20 HTML files and 10 forwards it went from 81 s to
   under 2 s. `--jobs 1` lays out inline.
6. **report.py + wordlist.py** — placeholders, cross-references, zip-contents
   sheets, the index, the exceptions table (front matter behind the index),
   START HERE, the DO NOT RELEASE pre-mark file, manifest.json, and the word
   list. A converted attachment gets no sheet of its own; only an attachment
   with no pages of its own does. START HERE ends with “Largest attachments”:
   one sentence on how much of the total the top 10 fill, then a short table.
   START HERE is kept back, so filenames are fine there.
7. **assemble.py** — plans Bates ranges and volume boundaries, then merges
   with pypdf, stamps every page (Form XObject overlay, correct for rotated
   pages), writes page labels, content-free bookmarks, and Title-only
   metadata. It raises if any input’s page count differs from the plan
   rather than let Bates numbers drift. `--no-bates` suppresses the stamp and
   the page labels; planning still assigns numbers (that is how volumes are
   split), but nothing prints them and `report` drops every page reference
   from the index, the exceptions table and the cross-reference pages. A high
   Bates number on a small production invites the requester to ask what else is being held back. `write_appendix` writes the
   appendix volumes the same way.
8. **premark.py** — optional. Finds terms and patterns with pdfplumber and
   adds `/Redact` annotations in place as an incremental update. Page content
   is never touched. Finding words (pdfminer, pure Python) is the slowest
   stage of a run, so pages are scanned in parallel chunks across a
   process pool; the annotations are still written in page order by the
   parent, byte-identical to `--jobs 1`. `embeddedcmap.py` teaches pdfminer
   to read fonts whose Encoding is an embedded CMap, which pdfminer cannot
   parse and decodes to nothing. WeasyPrint writes one whenever a font lacks
   a glyph (emoji, CJK), and one ✅ hid a page's regular text from premark.
   Codes are read through ToUnicode, widths by the code's CID.
9. **survey.py** — the page budget. `measure` turns the bundles and the plan
   into a `PageBudget`: distinct attachments by sha256, the slots that carried
   each (`copies`), the pages each puts in the volumes (once when deduped,
   since later copies are one-page cross-references; every time with
   `--no-dedup-attachments`), totals by type, and a what-if table for moving
   attachments longer than 10 to 500 pages out of the message volumes. START
   HERE, the manifest’s `largest_attachments` and `--survey` all read it.

`--survey` is the page count without the volumes, for when an archive might
run to tens of thousands of pages (mostly log files, printed at 59 lines a
page). It extracts, parses, organizes, lays out every message and
converts every attachment as a real run does, so every count is real, then
plans with the real index and exceptions table and stops. It writes
`Attachment survey.pdf`, `Attachment survey.csv` and the manifest, and no
volumes, stamps, word list, pre-marks or START HERE. A full run with the same
options lands on the same total from the same extraction, since the placeholder and cross-reference notes it does not
render are one page each. `--terms`, `--patterns`
and `--keep-natives` are ignored with a note.

`--triage` is for deciding which attachments to produce before any are
converted (`natives.py`). Nothing is converted: every distinct attachment is
written once, exactly as sent, to `Original attachments - DO NOT RELEASE/` as
`A0042 <name>` (IDs in order of first reference, assigned before decisions
are applied, so they never move between triage runs. An expanded zip gets no
ID, but its files do), and listed in `Attachment index - DO NOT RELEASE.csv`
with every message that carried it and the first one’s date, sender and
subject. Every slot gets a one-page pointer naming the ID (no
cross-references). The volumes are `Triage copy - Volume NN of MM.pdf`:
unnumbered, not pre-marked, no word list, with their own START HERE saying
nothing is for release or redaction. The reviewers mark rows `omit` with a note (or
`latest`) and the index goes back unchanged as `--decisions` to an ordinary
production run, which converts only what is produced. `--survey` and
`--appendix-over` are errors with it, and `--terms`, `--patterns` and
`--no-dedup-attachments` are ignored with a note. Hand conversion in Acrobat
was ruled out: it skips the sanitizing pass and leaves no record of what was
not produced.

`--decisions FILE` applies the reviewers’ decisions (`decisions.py`). The file is the
survey’s `Attachment survey.csv` (or a triage run’s attachment index) with
its `decision` column filled in, in Excel; rows are matched by `sha256` or by `filename` (any case, or a pattern
with `*` for a report whose name changes daily). Besides `produce` (the
default) there are two decisions. `latest`: the attachments a row covers are copies
of one recurring report, and only the copy from the most recent message is
produced. Every other copy is withheld: never converted, a one-page note in
each slot that carried it (naming the kept copy’s message; no dedup
cross-reference, so each message says why for itself, and says the copy was
“withheld at the reviewer’s direction”, or “at the direction of” the
`--withheld-by` name), a row in the
exceptions table, and a count in START HERE and the manifest, whose
`decisions` block records every rule, what it matched and what it kept.
A bad row stops the run before extraction; a rule that matches nothing is
warned about. A survey run with `--decisions` writes the CSV back with the
decisions carried on their rows, hand-added pattern rows appended, and an
`outcome` column. A few daily reports can fill nearly all of the appendix
pages. `omit`: the attachments a row covers are not produced. The row must
carry a note, which is all the one-page note in each slot and the exceptions
row say (whether an attachment is out of scope or not public is the reviewers’ call),
and a row with a `sha256` omits exactly those bytes, never other files that
share the name.

`--search-report PATH` puts the Purview search on START HERE (both kinds)
and the survey, in `_search.html`, and in the manifest (`searchreport.py`).
Purview’s export report (`Reports-….zip`, or the folder Safari unzips it to)
holds `Summary-<timestamp>.csv`: `"Name","Value","Description"` rows with
blank rows and one-cell headings between them, UTF-8 with a BOM, CRLF. Only
its named rows are read (Query, the case, search and export names, the
start and end times, which give the export's run time for billing, the
permissions filter), never `SubmittedBySmtp` or the item counts,
since one export is often several .pst files. The query is printed exactly
as recorded, and only `(c:s)` and `(c:c)` are explained, as Microsoft
documents them. A plain text file is the query alone. The wrapper mounts
only the Summary CSV of a folder. Test fixtures copy the layout with
made-up values: never commit a real report.

The index is rendered inside a plan loop: plan, render cross-reference pages
and the index, and re-plan if the index page count changed (Bates strings
have fixed width, so it settles in a round or two).

`--appendix-over PAGES` moves every converted attachment longer than PAGES
pages out of the message stream into `Attachments NN of MM.pdf` volumes,
written after the message volumes (`0` moves them all, for a messages-only
first pass). Every slot that carried one gets a one-page pointer instead of
the pages; with attachment dedup on, identical bytes are one appendix item
and duplicates point at the appendix, not at the first message. Each item
opens with a one-page header sheet naming the file and the messages that
carried it. v0.3 dropped cover sheets for inline attachments, but in the
appendix the context of the message is gone, so the header stays. Bates
numbering runs on from the last message page, bookmarks read “Item 14 ·
message 0012 · date” (never the filename), and premark covers the appendix
volumes. With the option off, output is byte-identical to a run without it.
The survey counts an appendix item’s pages once and each carrier as a
pointer, and with the option on it drops the what-if table, since the
cut-off is already chosen.

## Local development

PRIVACY.md and SECURITY.md make specific claims: no network code, the exact
container restrictions, every output file that holds personal data, the
LibreOffice limits, and what the sanitizing passes remove. A change to any of
those updates them in the same commit, so neither ever overstates.

Keep README.md in sync with what the tool does. Any change that adds,
removes, renames or changes the behavior of a command-line option, an output
file, or a feature also updates, in the same commit:

* README.md: the option list (grouped as in the wrapper’s `--help`), the
  output layouts, and the section describing the feature.
* The wrapper’s `usage()` in `pst2pdf.sh` and its argument parsing.
* The CLI’s `--help` text in `cli.build_parser`.
* The CLI-only options named under “use Docker directly” in README.md, for
  flags the wrapper does not pass on.

`tests/test_docs_sync.py` enforces the flag lists: every flag the wrapper
accepts must open an entry in README.md’s option list and in the wrapper’s
`--help` (and nothing else may), every flag it forwards must exist in the
CLI, and every CLI flag must be named somewhere in README.md. It cannot check
descriptions, so before committing such a change, also read
`./pst2pdf.sh --help` and `python -m pst2pdf.cli --help` against README.md.

`docs/README.html` (self-contained, for sending on its own) and
`docs/README.pdf` are generated from README.md, so every commit that edits
README.md also runs `.venv/bin/python scripts/readme_docs.py` and commits
both. It writes only what is stale. The HTML step needs markdown-it-py (the
dev extra), and the PDF step needs WeasyPrint, so on the host it renders the
PDF inside the pst2pdf image (`--network none`, only `scripts/` and `docs/`
mounted). Build the image first if there is none. Links to other repository
files are rewritten to GitHub URLs so they work outside the checkout. The
PDF records the SHA-256 of the HTML it came from as `/SourceSHA256`, and
`tests/test_docs_sync.py` fails when the HTML differs from a fresh render of
README.md or the PDF’s hash differs from the HTML’s.

## Output layout

```
output/
├── 00 START HERE.pdf
├── Volume 01 of 04.pdf        index pages, then the exceptions table, then messages with attachments
├── Attachments 01 of 02.pdf   only with --appendix-over: long attachments, each behind a header sheet
├── Redaction word list.txt
├── Pre-marked terms - DO NOT RELEASE.txt   only with --terms; per-term counts never go anywhere else
├── manifest.json
└── Original attachments - DO NOT RELEASE/   only with --keep-natives; never zipped
```

With `--triage`, a triage copy for the reviewers, all of it kept back:

```
output/
├── 00 START HERE.pdf                        what the folder is for and what to fill in
├── Triage copy - Volume 01 of 04.pdf        unnumbered, a pointer page naming the file ID per attachment
├── Attachment index - DO NOT RELEASE.csv    one row per file; goes back as --decisions
├── Original attachments - DO NOT RELEASE/   README.txt, A0001 <name>, … each distinct attachment once
└── manifest.json                            options.triage true, a “native_files” block
```

With `--survey`, only the page count:

```
output/
├── Attachment survey.pdf      projected pages and volumes, types, largest 25, what-if; kept back
├── Attachment survey.csv      every distinct attachment, most pages first, with its sha256
└── manifest.json              options.survey true, a “survey” block, no volumes
```

`--separate` (the v0.1 layout, one PDF per message mirroring the PST folders)
was removed. It is an error that points at `--triage`.

## Project layout

```
pst2pdf/
├── Dockerfile            # python:3.12-slim-bookworm + pst-utils + WeasyPrint libs + LibreOffice (nogui) + Noto Sans CJK + Noto Color Emoji
├── pyproject.toml        # hatchling; jinja2, weasyprint, pypdf, pdfplumber, reportlab, Pillow, extract-msg, icalendar
├── pst2pdf.sh            # wrapper
├── PRIVACY.md            # what the tool does with data, and how that is enforced
├── SECURITY.md           # how to report a vulnerability, what counts
├── docs/                 # DESIGN, PREMARK-VERIFICATION, samples/, README.html and .pdf (generated)
├── scripts/readme_docs.py  # README.md to docs/README.html and docs/README.pdf
├── src/pst2pdf/
│   ├── cli.py            # flags, build_volumes
│   ├── model.py          # shared dataclasses and Options
│   ├── extract.py  parse.py  calendar.py  organize.py
│   ├── render.py  templates/
│   ├── convert.py  report.py  wordlist.py  assemble.py  premark.py
│   ├── survey.py         # page budget: START HERE’s largest attachments, --survey
│   ├── decisions.py      # --decisions: the reviewers’ per-attachment decisions (latest, omit)
│   ├── searchreport.py   # --search-report: the Purview query from the export’s Summary CSV
│   ├── textpdf.py        # plain text and delimited tables straight to PDF (reportlab)
│   ├── embeddedcmap.py   # pdfminer reads embedded-CMap fonts (for premark)
│   └── natives.py        # original attachment files: --triage IDs, files and index; --keep-natives
└── tests/                # pytest; conftest.py builds .eml/.ics fixtures in-process
```

PDF rendering refuses all non-`data:` resource fetches (a custom WeasyPrint
`url_fetcher`), so remote images and trackers in mail bodies are never hit and
the container runs with `--network none`.

## Deferred for future versions

* **OCR** for image-only attachments and scanned PDFs, so Search & Redact can
  find text inside them (tesseract/ocrmypdf are available in bookworm).
* **Contacts** (`.vcf`) are still skipped and counted.
* **RTF-bodied messages** that readpst cannot convert fall back to the plain
  text body. A proper RTF pass is a future enhancement.
* **Nested attachments**: attachments of a forwarded message are listed on
  its header but not converted (one level only).
* **Memory**: PST retains upstream behavior. MBOX spools attachment bytes to disk;
  models and bodies remain in memory, so very large exports still need capacity planning.
