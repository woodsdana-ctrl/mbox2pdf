# Review messages in your browser

This interface helps you decide which messages belong in a records request
before creating PDF review volumes. Everything stays on your computer. You do
not need Gmail credentials or a connection to Google.

## Start here

1. Start Docker Desktop and wait for it to finish starting.
2. Download the project from GitHub (Code → Download ZIP), unzip it, and open a
   terminal in the folder containing `compose.yaml`.
3. On macOS or Linux run:

   ```bash
   bash mbox2pdf-review.sh
   ```

   This creates a protected local `review-data` folder, builds the image, and
   starts both services. On Linux it uses your user/group IDs for output ownership.
   If you have a corporate HTTPS build proxy, `PST2PDF_BUILD_CA` can name its
   trusted CA bundle; the build uses it without disabling certificate verification.

   On Windows, open PowerShell in the project folder and run:

   ```powershell
   New-Item -ItemType Directory -Force review-data
   docker compose up --build
   ```

   You can also use the Bash launcher in WSL with Docker Desktop integration.
   Docker Compose v2 is included in current Docker Desktop. The Windows commands
   are documented but have not been tested on a Windows host; the two-container
   application is tested on Linux with Docker Compose.

4. Open **http://127.0.0.1:8765** in your browser. Keep the terminal open.
5. Click **MBOX files**, choose one or several unzipped `.mbox` exports, and click
   **Import MBOX files**. Wait for local indexing. All messages start as Include.

The first build downloads dependencies. Later, you can start without any build
or dependency download using `docker compose up --no-build` (with the same user
ID settings as the launcher on Linux). Stop with Ctrl+C or `docker compose down`.
Closing the browser alone does not stop Docker. Do not delete `review-data` to
stop the app: it holds your sources, decisions, and outputs. After stopping both
services, back up that entire folder to protected storage before moving machines.

## Review and exclude

The counts at the top cover **all deduplicated messages**, regardless of filters.
The list shows date, sender, To/Cc/Bcc recipients, subject, attachment count, and
Include/Exclude status. Select a subject to read the full available body text,
message headers, attachment names, Gmail labels and all source archive positions.
Previews intentionally use text rather than email HTML: images, tracking pixels,
links, scripts and attachments cannot execute or make resource requests.
Original HTML and attachment bytes remain in the unchanged imported source.

Filter by date, sender, recipient, subject, subject/body text, Gmail label or
review status, then click **Apply filters**. Filter text uses case-insensitive
substring matching. Date bounds are inclusive in the review's display timezone;
messages without a known date do not match a bounded date filter. A Date without
a known timezone retains its wall date and is flagged. Filters only control the
list. They never make an inclusion decision, and unsent edits do not redefine the
currently displayed filtered result set.

- Check individual rows or click **Select this page** to select up to 25 visible
  messages. Changing pages or applying filters clears selection.
- **Select all filtered results** explicitly selects every result across all
  pages. The selection count says so, and a confirmation appears before a bulk
  decision covering that set.
- Enter an optional reason and click **Exclude selected**, or use the corresponding
  button in the reading pane. Exclusion reasons stay internal.
- Use **Restore selected to Include** or **Restore this message to Include** to
  restore messages. Restoring clears their current reason while preserving the
  previous action in the audit history.
- **Undo last decision change** reverses the most recent decision batch in that
  review, including its previous reasons. It works for both individual and bulk
  changes. Undo also persists, and earlier batches can then be undone in order.

There is **one decision per deduplicated review item, applying to all copies**.
A row number and Message-ID are never used as the decision key. The stable key
hashes the MBOX pipeline's strict content-and-attachment identity, so conflicting
versions sharing a Message-ID remain separate items. Source filenames, SHA-256,
internal copy identifiers, 1-based positions and labels remain traceable in the
reading pane and internal review records. Labels do not establish mailbox ownership.

## Resume safely

Decisions save automatically in `review-data/internal - DO NOT RELEASE/review.sqlite3`.
Reopen the browser and choose **Resume a saved review**, or let it reopen your
last review. Restarting the containers does not reset decisions. Reimporting the
same set of filenames and byte-identical sources opens the existing review.
A different name or changed archive bytes creates a distinct review rather than
silently carrying decisions over. Saved reviews retain their original indexing
timezone; generation can use another display timezone.

Each review is bound to a sorted manifest of source filenames and SHA-256 hashes,
including repeated source archives. Imported bytes are hash-checked on reuse,
before decision changes, at queueing, and before and after worker parsing and
production. Missing or changed sources block reuse/generation. Incomplete archives
or message parsing failures block browser import; use the CLI's exception workflow
to investigate the source. Do not edit imported MBOX copies or the SQLite database.
Browser requests from stale windows cannot overwrite newer decisions unnoticed;
refresh when the app reports a revision mismatch.

## Generate PDFs

Choose your request name, display timezone, conversation/chronological order,
Bates prefix/start, page limit, attachment handling and optional appendix threshold.
This screen only selects messages for PDF production. It does not add a redaction
step or collect redaction terms, patterns, or attachment-decision CSVs. Continue
your existing redaction workflow after generating the selected volumes. Existing
CLI attachment decisions and redaction features remain unchanged.

Click **Review generation totals**. Check the included/excluded totals and the
output settings, then click **Generate PDFs**. Generation takes an immutable copy
of those decisions; later edits apply to a later run. A background worker verifies
the sources and sends only included source copies into the existing pipeline,
which recomputes threads, indexes, Bates ranges, volumes, attachment conversions,
attachment decisions, deduplication, appendices and cross-references from that set.
An excluded carrier cannot become a cross-reference target; identical attachments
are still converted when another included message carries them. The browser remains
usable while the worker runs. A queued run means the worker has not yet started
it; keep both Compose services running. Use only one worker instance.

When generation completes, use **Download review volume ZIP**. It contains only
the generated message/appendix volume PDFs, never the review database, exclusion
reasons, audit snapshot, original MBOX, native attachments, START HERE, word list,
pre-mark details or manifest. These are **review PDFs**, not approved release files.
Excluded data is not passed into PDF rendering or metadata generation. **Included
replies can still quote excluded messages**: selecting messages is not redaction,
and quoted text is never automatically removed or rewritten. A human must review
the included content and apply/verify any necessary redactions and sanitization.

Each run is saved beneath:

```
review-data/internal - DO NOT RELEASE/runs/<run-id>/
  decisions - DO NOT RELEASE.json   immutable decisions, events, source hashes, options, totals
  output/                          volumes plus internal guides/reports
  review-volumes.zip                only for successful ordinary volume runs
```

The SQLite review history is the live exclusion log. The per-run JSON preserves
the decisions and that log used for that particular run. These can contain
excluded bodies/subjects/addresses as well as reasons and must stay internal.
The app exposes only the allowlisted volume ZIP as a download; do not distribute
the entire `review-data` or raw run output folder.

Attachment survey and triage modes use only included messages too. Their outputs
are internal and accessible in the run's local output folder; no volume ZIP is
offered. Survey counts pages without producing volumes. Triage preserves the
existing native-file/index workflow;
clear the appendix threshold for triage, since no attachments are converted.
If generation fails or the worker is interrupted, the run is marked failed, no
ZIP is offered, and you can generate a new run after fixing the problem. Existing
successful runs are not overwritten or repackaged after a decision change.

## Local security and limits

The browser service is published only on `127.0.0.1:8765`; do not change it to a
public bind address. It uses a Docker bridge network so the localhost port can be published; it has no outbound service clients. The conversion worker
uses `network_mode: none`, has no published ports and shares only local storage.
Neither service mounts the Docker socket. Both drop capabilities, disallow new
privileges, have read-only container filesystems plus temporary scratch storage,
and run as an unprivileged user by default. The worker retains existing PDF/HTML,
attachment-path, archive-expansion and executable-content protections.

HTTP uses Host/Origin checks, a per-server random mutation token, no CORS, no-store
responses and a restrictive Content Security Policy. Browser email previews are
always text, and never insert email markup into the document. There are no
external APIs, AI calls, analytics, CDN scripts or downloaded fonts. Assets are
bundled locally, and conversion resource fetching still refuses every non-data URL.
Loopback is not a multiuser login system: other users/processes on your computer
may access the service. Protect the computer and data folder. Storage is local,
not application-encrypted; use your organization's disk encryption and retention
practices. Shared/cloud-synced folders may upload your data through other software;
choose a local directory approved for these records.

Uploads stream to disk (20 GiB per-file limit). Parsing, models and review text
still need memory proportional to the export; attachment payloads use the existing
disk spools. Multiple saved reviews, uploaded source copies, audit snapshots and
PDF runs consume disk space. No automatic deletion or retention policy is applied.
PDFs without text/image-only scans still require the existing manual review/OCR
workflow, and attached-message conversion retains the upstream one-level limit.

## Developer verification

```
.venv/bin/python -m pytest tests/test_review.py -q
.venv/bin/python -m pip install -e '.[dev,browser]'
# Install Chromium locally, then set CHROMIUM_PATH if not found on PATH.
.venv/bin/python -m pytest tests/test_review_browser.py -q
```

The browser test imports synthetic archives, distinguishes page/all-filtered
selections, excludes/restores/undoes, reloads to resume decisions, generates PDFs,
checks downloadable text/metadata and observes all browser requests for external
resources. To run it against the actual Compose services, set
`MBOX_REVIEW_BASE_URL=http://127.0.0.1:8765`. Docker backend regression tests still
run with `--network none`; the optional browser test skips if its extra or Chromium
is absent. All fixtures are generated synthetic records, never real district mail.
