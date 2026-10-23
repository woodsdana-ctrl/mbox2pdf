# Security policy

pst2pdf opens untrusted files (every attachment in a mail archive) and
produces documents that people redact and release. A security problem here
can mean private information reaching the public, so reports are welcome.

## Supported versions

Fixes go into the latest version on the `main` branch. Older versions do not
receive fixes, so update before reporting if you can.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it
privately through GitHub instead: on the repository’s **Security** tab,
choose **Report a vulnerability**.

Include what you ran, what happened, and what you expected. A small sample
file that shows the problem helps most. **Never send a real mail archive** or
anything else containing real people’s information. Build a sample with
invented names and content.

This is a small project maintained on a best-effort basis. Reports are answered as
soon as possible, but there is no guaranteed response time. Once a fix is
released, you are welcome to be credited in its notes.

## What counts as a security issue

* **Content outside the visible page.** Any way for text from an archive to
  end up in a volume where Acrobat’s redaction cannot reach it: document
  metadata, bookmarks, link targets, alt text, form fields, embedded files,
  hidden text, or anything else redaction does not show.
* **A network connection during a conversion**, or any way for archive
  content to cause one.
* **Escaping the container’s limits.** A crafted archive or attachment that
  reads or writes files beyond what the script mounts, or gains privileges.
* **Kept-back material in a release.** For example, the original attachments
  in a production zip, pre-marked terms appearing in START HERE or the
  manifest, or the Purview query appearing in a volume.
* **Silent omissions.** A message or attachment missing from the volumes with
  no note or exceptions entry saying so. A production that looks complete but
  is not is a legal risk even when nothing leaks.
* **A crafted file that hangs a run or exhausts the machine** despite the
  time and size limits below.

## What does not

* **Vulnerabilities in LibreOffice, readpst, WeasyPrint, Debian or Python
  libraries** belong to those projects. Please report them there, and let
  us know if the image needs to pick up a fix.
* **The Docker runtime and the host operating system.**
* **Running the image without the restrictions the script applies.** See
  [PRIVACY.md](PRIVACY.md).
* **A pre-mark that misses a name.** Pre-marks are proposals, and the
  reviewer checks every page. That is still worth an ordinary issue.

## How attachments are handled

* The container runs with no network, no Linux capabilities, no privilege
  escalation, a process limit and your own user account. The archive and any
  terms, decisions or search report file are mounted read-only (from a
  report folder, only its Summary CSV).
* LibreOffice runs headless with a fresh profile for every file, with macros
  disabled, and is stopped after 120 seconds.
* An attachment over 100 MB is not converted, and one that converts to more
  than 2,000 pages is not included. Each gets a page saying why, and a line
  in the exceptions table.
* Attached PDFs and LibreOffice’s output are rebuilt page by page with
  scripts, form fields, annotations, embedded files and metadata removed.
* Message bodies lose scripts, style blocks, frames, forms and alt text.
  Links are printed as visible text, and remote resources are never fetched.

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
