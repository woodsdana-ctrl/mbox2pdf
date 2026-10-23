# Design: review volumes (v0.2)

Implementation contract for the review-volume layout (see “Why review
volumes” in README.md for the reasoning). Every module below owns a file, exposes the functions listed here, and communicates
only through the dataclasses in `src/pst2pdf/model.py`. Do not widen an
interface without updating this document.

Defaults: every feature sits behind a flag. Default order is by thread. Identical attachments are included
once with a cross-reference. Bates default is `DOC-` restarting at 1 per
request, with 1,000 pages per volume. Native attachment files excluded by default.

## Pipeline

```
extract.py   .pst ──readpst──▶ work/<folders>/*.eml, *.ics
parse.py     .eml/.ics ──▶ MessageModel          (calendar.py for .ics)
organize.py  [MessageModel] ──▶ deduped, threaded, ordered [(seq, model, folders, thread_id)]
render.py    MessageModel ──▶ message PDF        (parallel, unchanged mechanism)
convert.py   Attachment ──▶ ConvertedAttachment  (cached by sha256)
report.py    cover sheets, placeholders, cross-refs, index, START HERE, Exceptions, manifest
wordlist.py  [MessageModel] ──▶ Redaction word list.txt
assemble.py  [MessageBundle] ──▶ Plan ──▶ Volume NN of MM.pdf (merged, Bates, outlines, labels)
             [AppendixItem]  ──▶ Attachments NN of MM.pdf (only with --appendix-over)
premark.py   volume PDFs + term list ──▶ /Redact annotations added in place
survey.py    [MessageBundle] + Plan ──▶ PageBudget (START HERE, manifest, --survey)
cli.py       orchestration and flags
```

`cli.py` order of operations:

1. Extract. Parse every `.eml` and `.ics` into `MessageModel` (folder set from
   the relative directory).
2. `organize.organize(models, options)` → ordered list of `(model, folders, thread_id)`.
3. Render message PDFs in the worker pool (as today). Convert attachments in
   the parent through `convert.Converter` (LibreOffice runs are subprocesses,
   so the parent can run several at once with a thread pool. The converter is
   responsible for that).
4. Build `MessageBundle`s: assign `seq`, decide attachment dedup
   (`duplicate_of`), ask `report` for separator/placeholder/cross-ref pages.
   With `--appendix-over`, `cli._assign_appendix` then turns every slot whose
   attachment is too long into a pointer (`appendix_item`) and collects the
   `AppendixItem`s, and `report` renders each item’s header sheet.
5. `assemble.plan(bundles, options, front_matter_pages)` → `Plan`. Render the
   index with the plan’s ranges via `report.render_index`. If its page count
   differs from `front_matter_pages`, re-plan with the new count (at most
   three rounds. The Bates strings have fixed width so it converges).
   The appendix items ride along (`plan(..., appendix=items)`), and once the
   plan has settled the appendix pointer pages are rendered against it.
   Then `survey.measure(bundles, options, plan)` → `PageBudget`, kept as
   `stats["page_budget"]` for START HERE and the manifest.
6. `assemble.write_volumes(plan, bundles, index_pdf, options, output_dir)`,
   then `assemble.write_appendix(plan, items, bundles, options, output_dir)`.
7. `premark.apply(volume_paths, terms, options)` when a term list or patterns
   were given (appendix volumes included), then `report.write_premark_detail`
   for the per-term file.
8. `wordlist.write`, `report.write_start_here`, `report.write_manifest`.
   The exceptions table is not a file of its own: step 5 renders it as front
   matter behind the index.
9. (Removed: `--separate` wrote the legacy per-message tree. It is now an
   error pointing at `--triage`.)
10. With `--survey`, stop after the attachment counts of step 4: plan as in
    step 5 with the index and exceptions table laid out for real but no
    cross-reference or placeholder pages rendered (the plan counts each as one
    page; with `--appendix-over` the appendix is planned too, header sheets
    at one page each), then `survey.measure`, `survey.write_survey_pdf`,
    `survey.write_csv` and `report.write_manifest` with no plan and no
    volumes. Nothing of 5–8 is written: no volumes, stamps, word list,
    pre-marks or START HERE.

## Output layout

```
<output>/
├── 00 START HERE.pdf
├── Volume 01 of 04.pdf            (index pages, then the exceptions table, then messages)
├── Volume 02 of 04.pdf
├── Attachments 01 of 02.pdf       (only with --appendix-over: long attachments, one sheet each)
├── Redaction word list.txt
├── Pre-marked terms - DO NOT RELEASE.txt   (only with --terms or --patterns)
├── manifest.json
└── Original attachments - DO NOT RELEASE/   (only with --keep-natives; the wrapper never zips it)
```

With `--triage`:

```
<output>/
├── 00 START HERE.pdf                        (start_here_triage.html)
├── Triage copy - Volume 01 of 04.pdf        (unnumbered; a native_pointer.html page per attachment slot)
├── Attachment index - DO NOT RELEASE.csv    (natives.INDEX_COLUMNS; valid --decisions input)
├── Original attachments - DO NOT RELEASE/   (README.txt and "A0042 <name>" per distinct attachment)
└── manifest.json                            (options.triage true, a "native_files" block)
```

With `--survey`:

```
<output>/
├── Attachment survey.pdf          (projected pages and volumes, largest attachments, what-if)
├── Attachment survey.csv          (every distinct attachment, most pages first)
└── manifest.json                  (options.survey true, a "survey" block, no volumes)
```

## Module contracts

### parse.py

* `parse_eml(path, folder="") -> MessageModel`. Fills every field of
  `MessageModel`: `message_id`, `in_reply_to`, `references`, `thread_index`,
  `participants` (via `email.utils.getaddresses` over From/To/Cc, names
  stripped of quotes, addresses lowercased), `has_images`, `content_hash`.
* Body hygiene, applied to `body_html` before it leaves parse:
  * `<a href="X">text</a>` becomes `text` when X is a `mailto:` or the visible
    text already equals X. Otherwise `text [X]`. No `href` survives, so
    WeasyPrint never emits a link annotation.
  * `alt` and `title` attributes are removed from every element.
  * `<script>`, `<style>`, `<link>`, `<meta>`, `<iframe>`, `<object>`, `<form>`
    and `<input>` are removed.
  * Elements with `display:none`/`visibility:hidden` inline styles are left
    alone (WeasyPrint drops them). Do not try to be clever.
  * Everything must wrap to the page: text past the right margin is clipped
    from view but stays in the text layer. `white-space: nowrap`/`pre` become
    `normal`/`pre-wrap`; `nowrap` and `class` attributes, `min-width`, the
    `overflow-wrap`/`word-break` overrides, negative margins, `transform` and
    `overflow` go; table cells lose every width and other elements lose
    absolute widths (images keep theirs, capped by the template). A run of 30
    or more characters with no space (a link target) is wrapped in a span with
    `overflow-wrap: anywhere`; shorter runs, names included, break only at
    spaces.
  * `cid:` images are re-embedded as `data:` URIs as today, and `has_images` is
    true when any `<img>` remains.
* `content_hash`: sha256 over `date|from|to|subject|sha256(body_html)`.

### calendar.py

* `parse_ics(path, folder="") -> MessageModel` with `kind="calendar"`.
  Subject is the summary, From is the organizer, To is the attendee list,
  Date is DTSTART. Body is a small HTML block with When, Where, Organizer,
  Attendees, and the description. Uses the `icalendar` package. Attachments
  in an ICS (`ATTACH` with binary) become `Attachment`s.

### organize.py

* `organize(models, options) -> list[Organized]` where
  `Organized = (model, folders, thread_id)` as a small dataclass defined in
  organize.py.
* Message dedup (when `options.dedup_messages`): key is `message_id`, falling
  back to `content_hash`. Keep the first in sorted-source order. Collect every
  folder in `folders`.
* Threading: union-find over `message_id`, `in_reply_to`, `references`. Then
  merge groups whose Outlook `Thread-Index` shares the first 22 base64
  characters. Then merge groups whose normalized subject (strip `re:`, `fw:`,
  `fwd:`, `aw:`, whitespace, case) matches AND that share at least one
  participant address. `thread_id` is the sha256 of the earliest
  `message_id`/`content_hash` in the group, first 12 hex chars.
* Ordering:
  * `thread`: threads by earliest date, messages within a thread by date,
    undated last. Ties by source path.
  * `chrono`: by date, ties by source path.
  * `folder`: by `folder`, then date, then source path (today’s behavior).

### convert.py

* `class Converter(work_dir, options, render_message)`:
  * `convert(attachment) -> ConvertedAttachment`, cached by sha256 so the same
    bytes are converted once per run.
  * `render_message: Callable[[MessageModel], Path]` is supplied by cli.py so
    nested `.eml`/`.msg` attachments are rendered by the same template
    (attachments *of* nested messages are listed on that message’s header but
    not converted, one level only).
  * Dispatch by extension first, then by content type, then by magic bytes.
    An Office name (from either) is checked against the bytes before it is
    trusted: a legacy name (doc dot xls xlt ppt pps) needs the OLE2
    signature and an Office Open XML name a zip. Otherwise the bytes decide:
    another container format by magic, an HTML table as html, readable text
    (a byte-order mark, or no NULs and at least 97% printable) as text, and
    anything else still goes to LibreOffice, which reads old binary formats.
    Reporting systems export tab-separated UTF-16 text as `.xls`;
    LibreOffice printed each such row as one unbroken run of characters:
    * Office: doc docx dot dotx xls xlsx xlsm ppt pptx odt ods odp rtf wpd →
      `soffice --headless --convert-to pdf`, one fresh
      `-env:UserInstallation=file://<tmp>` per call so parallel runs do not
      collide, `timeout` from options, killed on expiry → placeholder
      “conversion timed out”.
    * Images: png jpg jpeg gif bmp tif tiff webp → Pillow → single- or
      multi-page PDF fit to Letter with margins, one page per frame.
    * pdf → pypdf pass-through that drops JavaScript, embedded files, form
      fields (flattened where possible, otherwise removed), and all
      annotations except none. Encrypted → placeholder “password protected”.
    * txt csv log md json xml → `textpdf.render_text_pdf` (reportlab): the
      page the WeasyPrint `<pre>` made (Letter, 0.75 in margins plus the
      6 pt body margin, DejaVu Sans Mono 8.5 pt, 59 lines, wrapped at spaces
      to 96 columns), written directly. Text with a character the font has
      no glyph for goes to WeasyPrint with `<pre>` instead, so nothing draws
      as an empty box. Delimited text prints as an aligned table instead
      (`textpdf.render_table_pdf`): tabs when at least 80% of the first 200
      lines split into the same number (two or more) of fields, commas only
      for a `.csv` or an export under an Office name. Columns with no values
      are left out and named in a note on the first page; the first row
      repeats atop every page only when it looks like a header (mostly
      filled, no number, date or address), so no one's data is printed on
      every page. Each column starts at its longest value (up to 60) and,
      when a row is too wide, gives way widest first but never below its
      longest word (up to 50), so cells wrap only at spaces and no name or
      address is split. Formats: 8.5 pt portrait, 8.5 pt landscape, 7.5 and
      7 pt landscape; a later one is taken only when it needs at most 90%
      of the pages. A table that fits no format without splitting words
      prints as plain text. Html htm → WeasyPrint after the same hygiene
      pass as parse (import the helper from parse).
    * eml → `parse.parse_eml`, msg → `extract_msg`, ics →
      `calendar.parse_ics`, each to a MessageModel rendered through
      `render.build_html` (or an injected `render_message`, as tests do).
    * Layout (reportlab text, WeasyPrint HTML and nested messages) runs in a
      forkserver `ProcessPoolExecutor` of `jobs` workers, created on first
      use; converter threads block on the result. With `--jobs 1`, or
      `convert.RENDER_IN_PROCESSES = False` in tests that stub
      `render.render_pdf`, it runs inline under one lock.
    * zip → placeholder listing the entry names and sizes (no extraction).
    * anything else, empty content, over `attachment_max_bytes`, or any
      exception → placeholder with a reason.
  * `page_count` filled from pypdf for every produced PDF.
  * Concurrency: the Converter may run LibreOffice conversions on a thread
    pool sized `options.jobs`. The cache must be thread-safe.
* Security: soffice runs with `--norestore --nolockcheck --nologo`, no macro
  execution (`-env:` + `--safe-mode` is not scriptable. Pass the
  `MacroSecurityLevel=3` config via a generated `registrymodifications.xcu` in
  the temp profile), cwd in the temp dir, and a `resource` RLIMIT_AS if it can
  be set. Never write outside `work_dir`.

### report.py + templates

All pages are rendered through Jinja2 + WeasyPrint using `render.render_pdf`,
Letter size, the same font as messages, `font-variant-ligatures: none`,
`hyphens: none`.

* `render_placeholder(entry, bundle, options, out)`: one page standing in for
  an attachment that could not be converted. It shows “Attachment 2 of 3”,
  filename, size, type, the reason, and the message it belongs to (date and
  subject). No page that can be released gives a message number: it would tell
  the requester how many messages were collected. The index, the exceptions
  table and the bookmarks keep them.
  A *converted* attachment gets no sheet of its own: its pages follow the
  message directly (v0.3; v0.2 put a cover sheet in front of each one).
* `render_crossref(entry, bundle, first_bundle, first_range, options, out)`:
  “Identical to the attachment at DOC-000412 to DOC-000451”, or “behind an
  earlier message in Volume 02 of 04.pdf” when `stamp_bates` is off, then the
  first copy’s message by date and subject.
  Because the referenced range is only known after planning, this page is
  rendered inside the plan loop. It is always exactly one page.
* `render_appendix_pointer(entry, bundle, item, plan, options, out)`: with
  `--appendix-over`, the one page standing in for an attachment moved to the
  appendix: “Its pages are in Attachments 01 of 02.pdf, at DOC-000412 to
  DOC-000451”, or “…, behind a sheet naming this file” when `stamp_bates` is
  off. It prints no item number and no count of carrying messages (“also
  attached to other messages”), since either tells the requester how much
  was collected. Rendered after the plan settles. Always one page.
* `render_appendix_header(item, bundles_by_seq, options, out)`: the header
  sheet in front of each appendix item (filename, size, type, pages, and the
  carrying messages by date and subject, the first five then “It was also
  attached to other messages”). No item number and no counts, as on the
  pointer. It quotes no page numbers, so it is rendered before planning.
  The index and the bookmarks still number items.
* `render_index(bundles, plan, options, out) -> Path`: table of seq, Bates
  range, date, from, to, subject, folder(s), attachment count, image flag.
  Landscape is acceptable. Repeat the header row on each page. With
  `stamp_bates` off the Bates column is dropped and the widths in
  `report._INDEX_WIDTHS` redistribute, because there is no printed number for
  it to quote.
* `write_start_here(output_dir, options, stats, plan) -> Path`: one page,
  request id, run date, counts (messages, deduped, attachments converted /
  cross-referenced / exceptions, volumes, Bates range), and the Acrobat
  steps. Include the pre-mark coverage table when premark ran, as counts per
  category only. No term text ever appears on START HERE.
* `render_exceptions(bundles, plan, options, out, render_failures, prepared)
  -> Path | None`: the exceptions table, rendered inside the plan loop as
  front matter behind the index. No Subject column: message number and Bates
  page identify the message, or the message number alone with `stamp_bates`
  off.
* `write_premark_detail(output_dir, coverage, terms) -> Path | None`: the
  per-term hit counts, in a file whose name says DO NOT RELEASE.
* `write_manifest(output_dir, options, stats, plan, volumes)`: JSON with
  version, request id, timestamps, counts, per-volume sha256 and Bates range,
  exceptions list, options used. Coverage appears as per-category counts and
  the sha256 of the terms file, never its path or contents. With
  `stats.page_budget` set it adds `largest_attachments` (the top 25 from
  `survey.manifest_rows`, sha256 included), and on a survey run a `survey`
  block with the projection, the per-type totals and the what-if rows.
* START HERE ends with “Largest attachments” when any attachment has pages:
  one sentence on how much of the total the top 10 fill, then filename, type,
  pages, copies, pages in the volumes and share (`survey.start_here_context`).
  START HERE is kept back, so filenames are fine there.
* `message.html` gains: folder line (all folders when deduped), message
  number, `kind == "calendar"` layout, and the hygiene CSS above. After
  v0.3 the number, folders and copy count are printed only in a triage copy
  (`cli._header_origin`): on a released page they tell the requester how
  much was collected, and a folder name is whatever the mailbox owner typed.
  Header values get `white-space: nowrap; overflow-wrap: normal` so a name never
  wraps mid-way. Long recipient lists wrap between addresses only.

### wordlist.py

* `write(models, output_dir) -> Path`: distinct display names and addresses
  from every model’s `participants`, one per line, names first then
  addresses, each sorted case-insensitively, UTF-8 with CRLF line endings so
  Acrobat on Windows imports it cleanly. Names shorter than three characters
  are dropped.

### assemble.py

* `plan(bundles, options, front_matter_pages) -> Plan`: walks bundles in seq
  order, consumes `message_pages`, then per attachment the stand-in page count
  (0 for a converted attachment, 1 for a cross-ref, placeholder or zip
  contents page) plus the converted `page_count`. Closes a volume when adding the next bundle would exceed
  `options.volume_pages`, unless the volume is empty (a single oversized
  bundle still gets its own volume). Bates numbers start at
  `options.bates.start`, front matter first. With `appendix=[AppendixItem]`
  a slot carrying `appendix_item` counts one page (its pointer), and the
  items are laid out after the last message page, in item order, header
  sheet then content, split into `Plan.appendix_volumes` by the same rule.
  `Plan.appendix_ranges` maps item number to range. With no items the plan
  is exactly what it was before the option existed.
* `write_volumes(plan, bundles, index_pdf, options, output_dir) -> list[Volume]`:
  pypdf merge in plan order. Bates stamp bottom-right of every page in 8 pt
  DejaVu Sans using a reportlab overlay generated per page size. PDF page
  labels equal to the Bates label. `Options.stamp_bates` false suppresses
  both. Planning still numbers every page, nothing is printed, and `report`
  leaves the numbers out of the index, the exceptions table and the
  cross-references. Outline: volume root → one entry per
  message titled `"{seq:04d} · {date:%Y-%m-%d %H:%M}"` (never subject or
  names) with child entries `"Attachment n"`. Document metadata: Title =
  `f"{options.request_id} — Volume NN of MM"`, Producer = `pst2pdf x.y`, no
  Author, no Subject, no Keywords. Fill `Volume.page_count` and return the
  volumes. Volume filenames: `Volume 01 of 04.pdf`.
* Must handle rotated and non-Letter pages from converted attachments: the
  stamp goes in the visual bottom-right of each page regardless of `/Rotate`.
* `write_appendix(plan, items, bundles, options, output_dir) -> list[Volume]`:
  the appendix volumes, `Attachments 01 of 02.pdf`, with the same stamping,
  page labels, Title-only metadata and page-count checks as `write_volumes`.
  Outline: volume root → one entry per item titled
  `"Item 14 · message 0012 · 2024-03-15 09:12"` (the first carrying message),
  never the filename.

### premark.py

* `load_terms(path) -> list[Term]` where a line is
  `[category]  text` or bare `text`, with `#` comments. Blank lines ignored.
  Categories are free text and become the overlay text through
  `options`-supplied mapping (default overlay is the category itself, or
  “REDACTED” when none).
* Name variants are opt-in with a suffix flag on the line: `[student] Jane Doe
  +variants` adds `Doe, Jane`, `J. Doe`, `Doe` (last name alone) but NOT the
  first name alone unless `+first` is given.
* `PATTERNS`: `phone` (US formats), `ssn`, `dob` (dates preceded by DOB/born/
  birth), `student-id` (regex from `options.student_id_regex`, default
  `\b\d{6,7}\b` is too loose, so the default is None and the pattern is only
  active when a regex is supplied).
* `apply(pdf_paths, terms, patterns, options) -> Coverage`: for each page,
  pdfplumber extracts words with boxes. Terms are matched case-insensitively
  on whole-word boundaries across word sequences on the same line. Each hit
  becomes a `/Redact` annotation (`/Subtype /Redact`, `/QuadPoints`, `/Rect`,
  `/OverlayText`, `/IC [0 0 0]`, `/C [1 0 0]`, `/Contents` = category) written
  in place with pypdf. Never touches page content. Returns hits per term and
  per pattern, and the list of terms with zero hits.
* Parallel scan: finding words is the slow part (pdfminer is pure Python,
  about 65 ms a page), so with `options.jobs` above 1 the pages of every
  volume are split into chunks (about four per worker, at least 25 pages) and
  scanned in a `ProcessPoolExecutor` of `jobs - 1` workers. The parent keeps
  one core and writes each volume’s annotations as soon as its chunks are in,
  in page order, so the files are byte-identical to a serial (`--jobs 1`)
  run. On a test archive of about 1,400 pages, on a laptop with performance
  and efficiency cores, pre-marking took about a third of the serial time; the
  floor is the efficiency cores, not chunk balance (smaller chunks with a cached document
  per worker were no faster).
* Coordinates: pdfplumber’s top-left origin must be converted to PDF’s
  bottom-left, and `/Rotate` respected.

### survey.py (added in v0.4)

* `measure(bundles, options, plan=None, front_matter_pages=0,
  render_failures=0) -> PageBudget`: the one aggregation both the START HERE
  section and `--survey` use. Distinct attachments are keyed by sha256 across
  every slot. `AttachmentSize.copies` counts slots (after message dedup),
  `full_copies` the slots that are not cross-references, and
  `pages_in_volumes = pages × full_copies`: pages once when deduped, pages ×
  copies with `--no-dedup-attachments`. Placeholders and zip containers have
  0 pages and are never in a top list. With a plan, each slot’s pages and the
  totals come from the plan, so they equal the volumes; without one, each
  note is one page. Sorted by `pages_in_volumes`, then pages, then first
  message. With `--appendix-over`, an appendix item’s pages count once, at
  its first carrier (`full_copies` counts items, not carriers); every
  carrying slot is a one-page pointer and each item adds a header sheet, both
  counted as notes; `total_pages` takes in `Plan.appendix_volumes`, reported
  apart as `appendix_pages`/`appendix_volumes`; and `what_if` is left empty,
  since the cut-off is already chosen.
* `what_if(attachments, total_pages, volume_pages, thresholds)`: for 10, 25,
  50, 100, 250 and 500 pages, the distinct attachments longer than the
  cut-off, their `pages_in_volumes`, and the pages left, assuming each full
  copy moved out leaves a one-page note. Volumes left is `ceil(pages /
  volume_pages)`, an estimate, since the real split is per message.
* `write_csv(budget, output_dir)`: `Attachment survey.csv`, UTF-8 with BOM and
  CRLF, every distinct attachment with rank, filename, type, method, pages,
  copies, pages_in_volumes, share_percent, first message and date, size,
  sha256 (the key a per-attachment decisions file would use), status and
  reason. A cell that Excel would read as a formula gets a leading quote.
* `write_survey_pdf(output_dir, options, budget, stats)`: `Attachment
  survey.pdf` from `templates/survey.html` (the `_base.html` conventions).
  Headline totals, types, the top 25, exceptions, the what-if table and how
  it was counted, in plain sentences.

### cli.py, pst2pdf.sh, README, CLAUDE.md

Flags: `--request-id`, `--order`, `--bates-prefix`, `--bates-start`,
`--volume-pages`, `--no-dedup-messages`, `--no-dedup-attachments`,
`--no-convert-attachments`, `--keep-natives`, `--terms FILE`, `--patterns
LIST`, `--student-id-regex`, `--survey`, `--triage`, `--decisions FILE`,
`--appendix-over PAGES`, `--withheld-by NAME`, plus the existing `--jobs`,
`--work-dir`, `-v`, `--version`. The wrapper grows the same flags and takes a
request name as the optional second positional argument.
With `--survey`, `--terms`,
`--patterns` and `--keep-natives` are ignored with a note on stderr, so the
survey can be run with the same command line as the real run.
`--triage` (`cli.triage_options`) implies no Bates stamp, no conversion, no
`--keep-natives` (it writes the originals itself), attachment dedup, and no
terms or patterns; it notes on stderr that `--terms`, `--patterns` and
`--no-dedup-attachments` are ignored, and refuses `--survey` and
`--appendix-over`, in the CLI and the wrapper. `build_volumes` applies
`triage_options` itself too, so a caller cannot get a numbered triage copy.

## Testing rules

* Unit tests must run on the host without WeasyPrint, LibreOffice, or
  readpst. Skip (not fail) when they are missing, as the existing render
  tests do. Pypdf, pdfplumber, reportlab and Pillow are pure Python or wheel
  installs and are always available. Test against them for real.
* The Docker image is the integration environment, and `pip install pytest` there
  needs the network flag dropped.
* Regression-test against a real archive kept outside the repository. Never
  copy its contents into tests, fixtures, or reports.

## Implementation notes (post-build)

Where the built code differs from the contract above, the code wins. These
are the deliberate departures, recorded so the contract stays honest.

* **`images.py`** (new): shared normalization. Inline images and image
  attachments larger than 2,200 px on the long side are downscaled, and
  anything over 1.5 megapixels is stored as JPEG. `has_images` is true only
  for embedded images at least 96 px in both directions, so signature logos
  and tracking pixels do not flag a message. HEIC/HEIF opens through
  `pillow-heif` when installed.
* **`Converter`** also exposes `submit()`, `convert_all()`, `close()` and the
  context-manager protocol. LibreOffice output is sanitized like attached
  PDFs. PDFs with only an owner password pass through decrypted. Caps not in
  the contract: 5 MiB for text/HTML, 200 image frames, 100 listed zip
  entries, 4 concurrent soffice instances.
* **`Organized` and `MessageBundle`** carry `copies`, the number of times the
  archive held the message, because a Microsoft Purview export holds the same
  message once per mailbox searched into one flat folder, so a folder list alone cannot
  show it. `cli.parse_tree` also strips readpst’s single top-level directory
  (named after the archive) from folder paths.
* **`Options.overlay`** maps a term category to the redaction overlay text
  (`--overlay student="FERPA"`, repeatable).
* **`assemble`** stamps by wrapping each page’s content in a Form XObject
  rather than `merge_page`, which was seven times slower and left orphaned
  streams. Page labels are written as one prefix-only entry per page because
  PDF label styles cannot zero-pad. The outline has a titled root and an
  “Index” entry.
* **`premark`** defines `Term` and `Coverage` locally, accepts pattern names
  or a name-to-regex mapping, writes an incremental update so page content
  is byte-identical, and adds `/DA`, `/Q`, `/Subj`, `/P` and an `/AP` outline
  so viewers other than Acrobat show the marks.
* **`report`** derives the exception list from bundles when given. The index
  shows display names only and trims To at four recipients, and the picture
  flag also covers image attachments.
* **Folder wording**: a Microsoft Purview export can put every message in one
  folder, so “found in other folders” became “additional copies of the same
  message”. The index drops its Folder column when every row shares one
  folder and says so once in its lead.
* **Front matter** is the index followed by the exceptions table, merged into
  one PDF inside the plan loop. `Exceptions.pdf` no longer exists. The outline
  still has a single “Index” entry for the front matter, because `assemble`
  exposes no way to add a second one.
* **`ConvertedAttachment.status == "container"`** is set by `cli`, not
  `convert`, on a zip that `archives.expand_all` expanded. It renders a sheet
  naming the attachment numbers its files occupy, counts as one page in
  `assemble.plan` (pdf_path is None), and is neither a conversion nor an
  exception. `RunStats.attachments_containers` counts them.
* **Render fallbacks**: when a message page fails in WeasyPrint, `cli` renders
  the same header with the body as escaped plain text and a notice, then a
  header-only page if that fails too. `RunStats.render_fallbacks` counts both,
  and header-only messages are listed as `render_failures` in the exceptions
  table and manifest. A message never vanishes from the volumes.
* **Off-page check**: `render.render_pdf` returns `Rendered(blocked, offpage)`,
  where `offpage` counts text and image boxes of the layout that lie outside
  their page. A message page with any is replaced by the plain-text fallback,
  whose notice says why (`RunStats.offpage_fallbacks`). Any other rendered
  page with any (attachments, report pages) is logged and counted in
  `RunStats.offpage_text`, which should be 0. Pass-through PDFs and
  LibreOffice output are not checked.
* **Times**: `cli.parse_tree` converts every aware date to the container’s
  local zone (`TZ`), and every date format shows the zone name. `calendar`
  converts the same way and its body carries only what the header cannot
  show: the end time (“Ends”) or the day span (“All day”). Every “prepared”
  line uses `stats.started`.
* **Default request id** is “Request” plus today’s date, never the archive’s
  file name, which lands in the PDF Title and could be a person’s name.
* **Word list** also emits a name without its parenthetical suffix and a
  “Last, First” name as “First Last”.
* **`premark.load_terms`** rejects a category whose text appears inside the
  term or any of its variants, since the category is stamped over the hit.
* **Appendix volumes** (`--appendix-over PAGES`, off by default). Built
  for a production that is mostly converted log files, with the
  messages buried between them. Any attachment whose conversion (status
  converted or passthrough) is longer than PAGES moves to
  `Attachments NN of MM.pdf`, written after the message volumes; 0 moves
  every converted attachment. Placeholders and zip-contents sheets always
  stay with their message. Decisions:
  * Every slot that carried such an attachment, the first copy included,
    gets a one-page pointer page. With attachment dedup on, identical bytes
    are one item and duplicates point at the appendix, never at the first
    message; with it off, every slot has its own item. The rule is decided
    per sha256, so a cross-reference never points at a moved slot.
  * Items are numbered in order of first reference in the message stream.
    Each starts with a one-page header sheet: v0.3 removed cover sheets
    because an inline attachment sits under its message, but in the
    appendix that context is gone.
  * Bates numbering carries straight on from the last message page, so one
    sequence covers the production, and the appendix splits by
    `volume_pages` with the same rules as the message volumes.
  * The index lists the appendix volumes in its volume table and gets a
    table of items (item, range when Bates is on, volume, filename, length,
    carrying messages, the first ten then “and N more”). START HERE lists
    the attachment volumes and says the message volumes can be redacted
    first. `manifest.json` gains `appendix_volumes` (with sha256) and
    `appendix_items`, only when the option is set; `counts` gains
    `attachments_appendix` (slots) and `appendix_items` (distinct items).
    A moved slot counts as neither converted nor cross-referenced.

### decisions.py (added in v0.5)

* `load(path) -> list[Rule]`: the rows of a survey CSV (comma-separated
  UTF-8 or cp1252 as Excel saves it, or tab-separated UTF-16 “Unicode Text”)
  whose `decision` column is filled in. Columns are found by name, any case:
  `decision`, `filename`, `sha256`, `note`. Decisions are `latest`, `omit`
  and `produce`; anything else, a decision on a row with neither filename nor
  sha256, or an `omit` row without a note, raises `ValueError` naming the
  file and line. `cli.main` loads the
  file once before anything else runs, so a typo costs seconds, not a run.
* `apply(entries, rules) -> Outcome`, on the organized messages after zip
  expansion and before conversion. A `latest` row covers the attachments
  whose name matches its `filename` (exact, any case; a pattern with `*`,
  `?` or `[`; the part after the last `/` counts, for files from a zip), or,
  with only a `sha256`, the attachments sharing that attachment’s name. Of
  the distinct copies it covers, the one whose latest carrying message is
  most recent (message date, then position) is kept; every other copy is
  withheld unless a `produce` row names its sha256. Rules apply in file
  order and an earlier rule’s decision stands. `Outcome` holds sha256 →
  `Withheld` (rule, report name, copies, kept sha256, the message showing
  the kept copy in full, its date and subject) and per-rule match counts and
  kept copies. `Withheld.reason(withheld_by)` says the copy was “withheld at
  the reviewer’s direction”, or “withheld at the direction of” the
  `--withheld-by` name (`decisions.withheld_at`); the exceptions table’s
  lead and the attachment’s status line use the same words. The reason is
  printed on the note page, so it says “an earlier copy” with no count of
  copies and names the kept copy’s message by date and subject; the
  exceptions table and the CSV give the count and the number.
* Withheld bytes get `ConvertedAttachment(status="withheld")` without being
  converted. `_build_bundles` never cross-references them, so every slot gets
  `templates/withheld.html` (via `report.build_withheld_html`), and
  `list_exceptions` lists them with `withheld=True` and the one-line
  `ConvertedAttachment.summary` (“Withheld: an earlier copy of a recurring
  report (30 copies); the latest is attached to message 31 (March 3,
  2026)”), not the note’s full reason: with the full reason and the
  reviewers’ note on every row, hundreds of withheld copies would make a
  table of many pages. The
  table (`report.group_withheld`) then folds a report’s copies into one row
  where the first copy was (“Withheld: 30 attachments, earlier copies of a
  recurring report, each replaced by a note where it was attached (messages
  1 to 30); the latest is attached to message 31 (…)”; attachments, since
  one copy can ride on several messages); the manifest’s exception list
  keeps one entry per attachment. Dates
  are always the date of the message named, which shows the kept bytes in
  full (a later forward of the same file cross-references it). Counts:
  `attachments_withheld`. The manifest’s `decisions` block has the file’s
  sha256 and, per rule, its line, decision, filename, sha256, note,
  attachments matched, and sha256s kept and withheld.
* `survey.build_csv(budget, rules, outcome)` adds `decision`, `note` and
  `outcome` columns: a sha256 rule’s decision stays on its row, filename and
  pattern rules are appended as rows of their own, and `outcome` says
  “produced: the latest of N copies” or “withheld: an earlier copy; the
  latest of N is attached to message M” (each `Withheld` or `Omitted`
  gives its own through `outcome()`). Fed back unchanged, it yields the
  same rules.
* `omit` (added with `--triage`): the attachments a row covers are not
  produced. A row with a `sha256` covers exactly those bytes, so the row for
  one `Budget.xlsx` never omits another file of the same name; only a row
  with no `sha256` matches by `filename` or pattern. A `produce` row with a
  `sha256` exempts those bytes, and an earlier rule’s decision stands, as
  for `latest`. `Omitted(rule)` has the same `reason()`, `summary()` and
  `outcome()` as `Withheld`: the reason is the reviewers’ note as written (a full
  stop added when it has none), the summary `Not included: {note}`. The
  note is required because it is the only explanation the pages give:
  whether an attachment is out of scope or not public is the reviewers’ call, so the
  tool adds no wording of its own. The slots take the withheld path
  (`ConvertedAttachment.decision` is `"omit"`): `withheld.html` shows “Not
  included.” and the note, `group_withheld` folds rows with the same note
  into one (“Not included: 6 attachments (messages 1 to 6), each replaced by
  a note where it was attached. {note}”), the exceptions table’s lead counts
  them apart from `latest` copies, START HERE says they were not included as
  directed, and the manifest counts `attachments_omitted` and gives each
  exception a `decision`.

## MBOX intake contract (mbox2pdf adaptation)

`mbox.discover` accepts a single `.mbox` or recursively finds `.mbox` files in
a directory. `mbox.read(source, work, stats, display_timezone)` uses stdlib MBOX
boundary indexing over an rb handle, never locks or writes an archive, and parses
one message at a time through shared `parse.parse_message`. A small read-only
adapter initializes mailbox's single-file reader state because its public
initializer otherwise tries rb+. Compatibility is tested on Python 3.12.
MBOX disables readpst-specific RTF-body suppression and generated-calendar rewriting,
so those attachment names and bytes are preserved exactly.
Original EML bytes and decoded attachments are retained only in protected work
storage. `DiskAttachment` supplies the Attachment interface (filename, content,
content_type, size, sha256); its content property reads disk on demand and size
and hash never materialize the payload. Expanded ZIP entries are spooled per
message before the next expansion. Models/bodies and envelope indexes still
scale with message count; this is not a constant-memory external sort.

`MessageModel` additionally records Bcc, original Date text and parsed timestamp,
source_format, identity_fingerprint, source_associations, gmail_labels and
intake_issues. Associations contain absolute archive path, 1-based message
position and the labels on that copy, never inferred mailbox ownership.
MBOX labels are CSV-parsed from X-Gmail-Labels, optional and independent of folders.
`parse._outer_parts` stops at message/rfc822 attachments; nested messages retain
their entire MIME tree instead of leaking their parts into the outer message.
Both body alternatives remain in EML, while the preferred body is sanitized for
rendering. Attached-message rendering retains upstream's one-level limit.

MBOX identity hashes relevant decoded headers plus the recursive MIME tree and
decoded payload hashes (see README for the exact list). Dedup merges all source
associations and labels; same-ID conflicts retain all variants and create intake
exceptions. MBOX threading uses reference identifiers only; ambiguous conflicting
IDs do not join groups. PST's legacy body/Safe Links fingerprint and subject/
Thread-Index heuristics remain in effect only for PST.

`Options` adds input_format, display_timezone, export_source, search_query and
export_description. CLI auto-detects directories and `.mbox`; PST still uses
readpst. --export-source, query and description are user declarations, never
export metadata proof. --search-report is restricted to PST/Purview. No Vault
companion metadata parser exists. Reports share `_search.html` for accurate
MBOX source context. Original aware timestamps are preserved before display
conversion; naive/malformed dates are flagged. Intake issues are visible on
message pages and through the existing exceptions renderer. Internal intake
JSON covers every organized message even in survey mode. Manifest input contains
archive completion observations, and volume manifests contain per-message
provenance. Fatal archive errors retain readable archives' output, set a nonzero
status and write an INCOMPLETE marker. Recoverable parsing errors produce a
placeholder, exception and nonzero status. No export is asserted complete.

All old conversion, rendering, attachment decisions, dedup cross-references,
appendix, Bates and proposed-redaction paths are reused. Production ZIPs exclude
internal reports as well as original attachments. No output is release-approved.
