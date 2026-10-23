# Privacy

pst2pdf converts mail archives that are full of personal information. This
page says what it does with that information and, just as important, how you
can check.

## The short version

pst2pdf does not send your data anywhere. It has no telemetry, no analytics,
no update checks and no outbound API clients. The optional browser interface
serves local HTTP only; its conversion worker is isolated from networking.
Run with `pst2pdf.sh`, conversion works inside
a container that has no network connection at all, so nothing inside it
could send data even if it tried. Everything it produces is written to the
output folder you choose, on your own computer.

## How that is enforced

Each point below is a property of the code, not a promise, and each one can
be checked (see “Checking for yourself”).

* **No network during a conversion.** `pst2pdf.sh` starts the container with
  `--network none`. The container has no network interface except its own
  loopback, so no code inside it, including LibreOffice and every library the
  tool uses, can reach the internet or your local network.
* **Nothing in a message is fetched.** Email bodies often point at remote
  images and tracking pixels. The renderer refuses every resource that is not
  embedded in the message itself, so converting an archive never tells a
  sender that their message was opened.
* **No outbound service calls.** Conversion code has no external API client.
  The optional review server listens for local browser requests and serves only
  bundled assets and local review data. Its conversion worker has no network.
  Building the Docker image downloads dependencies (see “Where the guarantees stop”).
* **CLI mounts are limited.** The archive, and a `--terms`,
  `--decisions` or `--search-report` file when you give one, are mounted
  read-only. From a Purview report folder only the Summary CSV is mounted,
  not the per-item and per-mailbox reports beside it. The output
  folder and temporary scratch storage are writable. Nothing else on your computer is
  visible inside it.
* **Temporary files are removed.** Intermediate files live inside the
  container and are deleted with it when the run ends.

## What pst2pdf writes, and where

CLI deliverables go to the output folder. Browser sources, decisions and runs
use the separate internal review-data folder described below. Much of it holds the
same personal information as the archive, unredacted, and needs the same
care:

* **The volumes** (`Volume 01 of 04.pdf` and the rest) contain every message
  and attachment, unredacted until your reviewers redact them.
* **`Redaction word list.txt`** lists every sender and recipient name and
  address.
* **`Pre-marked terms - DO NOT RELEASE.txt`** (with `--terms` or
  `--patterns`) lists the names and terms you asked to have marked, with
  counts.
* **`00 START HERE.pdf`** and **`Attachment survey.pdf`** name the largest
  attachments, and with `--search-report` they show the Purview query, which
  often names people, with the case, search and export names.
* **`manifest.json`** records counts, file hashes and the options used. It
  can include attachment file names, the reviewers’ notes, and with
  `--search-report` the Purview query and the case, search and export names.
  The address of whoever ran the export is never read from the report.
* **`Original attachments - DO NOT RELEASE/`** (with `--keep-natives` or
  `--triage`) holds the attachments exactly as they were sent.
* **`Attachment survey.csv`** (with `--survey`) and a triage run’s
  **attachment index** list attachment file names, and the index adds the
  sender, date and subject of the messages that carried them.

`--zip` packs the output folder into a zip beside it. That zip never includes
the original attachments, except for a `--triage` run, whose zip is named
“DO NOT RELEASE”.

The script’s messages in your terminal can include attachment file names in
warnings and errors (and, with `-v`, in every log line), so your terminal’s
scrollback may hold them too.

## Where the guarantees stop

* **Building the image uses the internet.** The first run downloads a Python
  base image from Docker Hub, system packages from Debian and Python libraries
  from PyPI. Those downloads carry nothing from your archives, and they finish
  before any archive is opened.
* **The protection depends on how the container is started.** `pst2pdf.sh`
  always starts it with no network. It also drops every Linux capability,
  blocks privilege escalation, limits the number of processes and runs the
  container as your user account. If you run the image with `docker run`
  yourself, include `--network none`, as the README’s example does. A
  hand-written `docker run` gets the other restrictions only if you add them.
* **Your Docker runtime is not part of pst2pdf.** Docker Desktop and other
  runtimes can collect their own usage data under their own privacy policies.
  pst2pdf has no say in that, so check your runtime’s settings if it matters
  to you.
* **pst2pdf does not redact anything.** It makes redaction faster. Pre-marked
  terms are proposals that a reviewer applies in Acrobat, and a volume is not
  ready for release until the reviewer has applied the redactions and
  sanitized the file, as `00 START HERE.pdf` describes.

## Checking for yourself

* Read `run_container()` in `pst2pdf.sh` for the exact `docker run` command.
* Search the source for network libraries. This prints nothing:

  ```
  grep -rnE '^\s*(import|from) (socket|urllib|http|requests|ssl|smtplib|ftplib)' src/
  ```

* While a conversion is running, find the container with `docker ps` and
  confirm it has no network:

  ```
  docker inspect --format '{{.HostConfig.NetworkMode}}' <container-id>
  ```

  It prints `none`.

If you find anything in pst2pdf that sends data, or that leaves a copy of
archive content somewhere redaction cannot reach it, please report it as a
security issue, as described in [SECURITY.md](SECURITY.md).

## MBOX adaptation

Offline MBOX conversion uses the same no-network rendering and container
restrictions as PST. A directory input mounts the whole dedicated directory
read-only; a single-file input mounts only that file. Never put output inside
an input directory. Gmail labels, Bcc, source paths, original timestamps,
user-supplied search queries and exceptions are sensitive. Keep manifest.json,
Intake report - DO NOT RELEASE.json, any INCOMPLETE notice, work-directory EML
and attachment spools, START HERE, the word list and originals internal.
Production ZIPs now exclude these reports and originals; triage ZIPs remain
entirely internal. Review volumes still contain unredacted visible content until
a human applies and verifies redactions. Temporary spools are removed normally;
--work-dir retains them and needs protected storage. Remote resources are blocked,
including tracking pixels and linked Drive files. No Google credentials are used.

## Browser review interface

The optional local HTTP interface is documented in [docs/REVIEW.md](docs/REVIEW.md).
It exposes only localhost port 8765 through Docker, with a Docker bridge
network for the UI and a separate worker using network_mode: none. Neither container has a
Docker socket mount. Existing CLI conversions remain network-isolated. This adds
local HTTP code, not external API/AI/analytics calls. All browser assets are local.
Email previews are inert text via textContent, with no attachment opening or image
loading; Host/Origin checks, mutation tokens, no-store responses and restrictive
CSP block cross-site requests and resource loads. PDF rendering keeps its existing
non-data URL restrictions. Source archives are copied, not modified, and hashes
bind review decisions to exact sources. Internal state includes exclusion reasons,
message text and addresses, source metadata and immutable generation audit records
under review-data/internal - DO NOT RELEASE/. Do not release this folder or raw
run output. Only allowlisted generated volume PDFs enter browser download ZIPs.
Excluded data is filtered before PDF production, but included replies may still
quote excluded material; selection never rewrites quoted text. All PDFs still
require human review and redaction. Loopback access is not multiuser authentication;
protect the local machine, disk and backups. No application encryption or automatic
retention/deletion is provided. Keep the port bound to 127.0.0.1.
