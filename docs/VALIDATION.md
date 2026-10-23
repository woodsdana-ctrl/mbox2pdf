# Validation — 2026-10-23

This fork starts from Minneapolis Public Schools' pst2pdf commit
`7649b57256173f1c460c649878d06388c82a48f7`. LICENSE and NOTICE are byte-for-byte
unchanged. The selected mbox2pdf checkout was initially empty; upstream files
were imported and extended. Validation preceded the GitHub publication step.

## Verified

- Full final Docker suite: **573 passed, 2 skipped**, in 88.22 seconds.
  Both skips are wrapper ZIP tests because the integration image lacks the host
  `zip` utility. Those tests pass on the host: wrapper and documentation suite,
  **40 passed**. The actual final wrapper ZIP was also inspected successfully.
- MBOX/report/survey focused tests: **98 passed**, including separate reporting
  of intake warnings versus true rendering failures.
- Final synthetic Docker conversion through `mbox2pdf.sh`, with network disabled,
  capabilities dropped, no new privileges and a PID limit: four source messages
  across two archives became three distinct messages in two volumes, eight
  searchable pages labeled TEST-000101 through TEST-000108. Three attachment
  slots became one converted text attachment, one cross-reference and one
  visible unsupported-file exception. The missing Date produced a distinct
  intake exception, not a claimed rendering failure. Duplicate source
  associations were retained. Eight proposed /Redact annotations were present.
- The final wrapper's production ZIP contains the review volumes and excludes
  the manifest, intake JSON, START HERE, the word list, pre-mark detail and
  original attachment files. Original attachment preservation was enabled for
  this check, so exclusions were checked against actual generated files.
- Generated README HTML/PDF consistency, shell syntax, installed CLI help,
  dependency consistency, source/image fingerprint agreement and original
  LICENSE/NOTICE equality all passed.
- The saved installation script was executed successfully, including repeated
  installation/builds. No persistent server process is required. Configuration
  fields saved: install_script and start_skill. Publication and restoration
  into a new task have not been performed.

Full integration command, from the checkout:

```bash
docker run --rm --network none --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 512 \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONPATH=/app/src \
  -v /workspace/mbox2pdf:/app:ro --entrypoint /usr/bin/tini \
  mbox2pdf-tests -- python -m pytest -p no:cacheprovider -q -rs /app/tests
.venv/bin/python -m pytest tests/test_wrapper.py tests/test_docs_sync.py -q
```

The development test image and virtual environment can be recreated with the
saved install script (also /workspace/.mbox2pdf-setup/install.sh). Local logs and
synthetic smoke outputs are outside the deliverable checkout, under
/workspace/.mbox2pdf-setup/evidence and /workspace/scratch. No mail archives or
review ZIPs are included in the source tree.

## Limits and unrun checks

- No binary PST end-to-end archive fixture was available. Existing PST extraction
  invocation tests, parsed-tree regression tests and shared workflow tests passed;
  the Docker image retains readpst and PST support.
- Acrobat review/application of proposed marks was not exercised. Marks do not
  remove content; a human must review, apply and verify redactions before release.
- The host's full upstream suite has Office/font-specific failures: the exposed
  Office shim fails the wide-sheet check, and host fonts/cache differ from the
  Docker environment. These checks pass in the supported Docker image. Use
  Docker for complete integration validation.
- Attached messages preserve their full original MIME contents in EML. Their
  immediate message is rendered, and its own attachments are listed but not
  recursively converted, matching the upstream one-level renderer.
- MBOX parsing and attachment spooling avoid retaining an export's complete
  attachment bytes in RAM. Models, body HTML/CID images and the envelope index
  still scale with export size; an individual large message and concurrent
  conversions still need memory. This is not a constant-memory external sort.
- MBOXO escaping is supported. MBOXRD extra quoting is retained. Improperly
  unescaped body lines beginning `From ` are inherently ambiguous under MBOX;
  use correctly exported archives. No Vault companion metadata parser exists.

## Local browser message selection (2026-10-23)

The browser adds inclusion/exclusion decisions before the existing PDF pipeline.
It adds no redaction controls and leaves the existing attachment-decision and
redaction implementations unchanged.

- Supported Docker integration suite: **588 passed, 3 skipped**. The skips were
  the optional browser dependency and two host ZIP-wrapper checks.
- Host wrapper and documentation checks: **40 passed**, including those ZIP
  checks. Focused selection, browser and documentation checks: **25 passed**.
- Chromium against the actual Compose services: **1 passed**. Synthetic multiple
  archives exercised page versus all-filtered bulk exclusion, undo, persistence,
  immutable generation decisions, included-only PDFs and package download.
- Restarting the containers retained decisions and completed runs, with source
  hashes verified. Docker inspection confirmed the conversion worker uses
  `network_mode: none`, read-only root filesystems and a localhost-only published
  browser port. Browser requests remained local; previews use inert text.
- Existing redaction regression tests passed. Tests reject newly supplied
  redaction settings in the selection API. No source mail or generated review
  packages are committed.

Windows launch instructions are documented but were not exercised. The browser
preview displays message text and attachment names; it does not open attachments.
Local review state and audit records contain sensitive source information and
must remain internal. Included replies can still quote excluded messages, and
PDFs still require human review and the existing redaction process as needed.
