#!/usr/bin/env bash
# pst2pdf.sh: friendly front door for the pst2pdf Docker container.
# Finds a docker CLI, builds the image on first use, and shows live progress.
set -uo pipefail

IMAGE=pst2pdf
# Resolve symlinks so the Docker build context is the real repo, even when
# this script is invoked via a symlink on PATH.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
    GREEN=$(printf '\033[32m'); RED=$(printf '\033[31m'); RESET=$(printf '\033[0m')
else
    BOLD=""; DIM=""; GREEN=""; RED=""; RESET=""
fi

usage() {
    cat <<EOF
${BOLD}pst2pdf${RESET} - convert MBOX exports or an Outlook .pst archive into PDF review volumes for redaction

Usage: $(basename "$0") [options] ARCHIVE.pst|ARCHIVE.mbox|MBOX_DIRECTORY ["Request name"]

Messages and their attachments become a few large, Bates-numbered PDF
volumes with an index, a START HERE page, and a word list for Acrobat's
Search & Redact. The request name appears on the START HERE page and in
the PDF titles (default: "Request" and today's date, never the archive's
file name, which could be a person's name).

Options:
  -o, --output DIR       Output directory
                         (default: a "<archive-name>-pdfs" directory beside the archive)
  -f, --force            Allow converting into a non-empty output directory
  -j, --jobs N           Work on N messages in parallel (default: one per CPU core)
  -v, --verbose          Show every log line instead of the progress counter
      --rebuild          Rebuild the Docker image even if it is current (it is rebuilt
                         automatically after an update)
      --zip              Also write a zip of the output directory beside it (without
                         the unredacted original attachments, except with --triage)
      --search-report PATH  Show the Purview search on the START HERE page, exactly as
                         Purview recorded it: the export's report (the Reports zip, the
                         folder it unzips to, or its Summary CSV), or a text file
                         holding only the query
      --input-format TYPE   auto (default), pst, or mbox
      --display-timezone ZONE  IANA timezone, e.g. America/Chicago
      --export-source TYPE  User-supplied MBOX origin: unknown, takeout, vault
      --search-query TEXT   User-supplied query; does not establish export completeness
      --export-description TEXT  User-supplied description for internal reports
  -h, --help             Show this help and exit

Layout:
      --order MODE       Message order: thread (default), chrono, or folder
      --no-bates         Leave the pages unnumbered: no Bates stamp in the footer,
                         and no page references in the index or exceptions table
      --bates-prefix P   Page number prefix (default: DOC-)
      --bates-start N    First page number (default: 1)
      --volume-pages N   Pages per volume before starting a new one (default: 1000)
      --appendix-over N  Move converted attachments longer than N pages out of the
                         message volumes into separate "Attachments 01 of 02.pdf"
                         volumes, with a one-page note in their place behind each
                         message (0 moves every converted attachment)
      --triage           Decide which attachments to produce: convert none, write each
                         once as the original file, and list them in "Attachment index -
                         DO NOT RELEASE.csv" for the reviewers to fill in and pass back with
                         --decisions. The volumes are unnumbered triage copies, not for
                         redaction. Ignores --terms and --patterns
      --survey           Count pages only: convert everything as a full run would,
                         then write "Attachment survey.pdf" (projected pages and
                         volumes, largest attachments, what setting long ones aside
                         would save) and "Attachment survey.csv" instead of volumes.
                         Ignores --terms, --patterns and --keep-natives

Content:
      --no-dedup-messages     Keep every copy of a duplicated message
      --no-dedup-attachments  Repeat identical attachments in full instead of cross-referencing
      --no-convert-attachments  Do not convert attachments to PDF pages (one naming page each)
      --keep-natives          Also write the original attachment files to a separate folder
                              ("Original attachments - DO NOT RELEASE", never zipped)
      --no-expand-zips        Leave zip attachments unexpanded (their contents are listed)
      --decisions FILE        The reviewers' decisions: "Attachment survey.csv" from a --survey run,
                              or the attachment index from a --triage run, with its
                              decision column filled in. "latest" keeps only the most
                              recent copy of a recurring report; the earlier copies are
                              withheld, each with a one-page note. "omit" (with a note)
                              leaves the attachment out, the note printed in its place
      --withheld-by NAME      Who withheld the earlier copies a "latest" decision covers,
                              as their notes say: "withheld at the direction of NAME"
                              (default: "withheld at the reviewer's direction")

Pre-marked redactions (optional):
      --terms FILE       Mark every occurrence of the names/terms in FILE for redaction
      --patterns LIST    Also mark patterns: phone, ssn, dob, student-id (comma-separated)
      --student-id-regex RE   Regular expression for the student-id pattern

The first run builds a Docker image, which takes a few minutes; later runs
reuse it. Conversion runs with no network access: remote images in messages
are deliberately not fetched.
EOF
}

die() { printf '%serror:%s %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$1"; }

# A fingerprint of everything the image is built from: the SHA-256 of each
# file in src/ (bytecode and Finder litter aside, since running the tests
# leaves __pycache__ behind), the Dockerfile and pyproject.toml, listed by
# path, hashed together. README.md is copied into the image too, but only as
# package metadata, so editing it does not force a rebuild. Prints nothing
# when there is no SHA-256 tool, and then only the revision label is checked.
source_fingerprint() {
    local sum
    if command -v sha256sum >/dev/null 2>&1; then sum="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then sum="shasum -a 256"
    else return 0
    fi
    (
        cd "$SCRIPT_DIR" || exit 0
        { printf '%s\n' Dockerfile pyproject.toml
          find src -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '.DS_Store'
        } | LC_ALL=C sort | tr '\n' '\0' | xargs -0 $sum | $sum | cut -d' ' -f1
    )
}

# The whole run is one function, called on the last line. bash reads a script
# as it executes it, so rewriting this file in place during a long conversion
# (an editor or a tool saving it; git replaces files, which is harmless) made
# the wrap-up read from a stale offset and fail with a syntax error. A
# function body is parsed whole before it runs.
main() {
    # --- parse arguments ------------------------------------------------------
    INPUT="" REQUEST="" OUTPUT="" JOBS="" VERBOSE=0 FORCE=0 REBUILD=0 ZIP=0
    TERMS="" DECISIONS="" SEARCH="" SURVEY=0 TRIAGE=0 APPENDIX=0
    PASS=()   # flags forwarded to the container unchanged
    need_arg() { [ $# -ge 2 ] || die "$1 needs an argument"; }
    while [ $# -gt 0 ]; do
        case "$1" in
            -h|--help) usage; exit 0 ;;
            --zip) ZIP=1; shift ;;
            --input-format|--display-timezone|--export-source|--search-query|--export-description|--order|--bates-prefix|--bates-start|--volume-pages|--patterns|--student-id-regex|--withheld-by)
                need_arg "$@"; PASS+=("$1" "$2"); shift 2 ;;
            --terms) need_arg "$@"; TERMS=$2; shift 2 ;;
            --decisions) need_arg "$@"; DECISIONS=$2; shift 2 ;;
            --search-report) need_arg "$@"; SEARCH=$2; shift 2 ;;
            --appendix-over)
                need_arg "$@"
                case "$2" in
                    ''|*[!0-9]*) die "$1 needs a whole number of pages, 0 or more (got \"$2\")" ;;
                esac
                APPENDIX=1; PASS+=("$1" "$2"); shift 2 ;;
            --survey) SURVEY=1; PASS+=("$1"); shift ;;
            --triage) TRIAGE=1; PASS+=("$1"); shift ;;
            --separate)
                die "--separate was removed: the one-PDF-per-message layout is gone. For attachments as separate files, use --triage." ;;
            --no-bates|--no-dedup-messages|--no-dedup-attachments|--no-convert-attachments|--keep-natives|--no-expand-zips)
                PASS+=("$1"); shift ;;
            -o|--output) [ $# -ge 2 ] || die "$1 needs a directory argument"; OUTPUT=$2; shift 2 ;;
            -j|--jobs)
                [ $# -ge 2 ] || die "$1 needs a number argument"
                case "$2" in
                    ''|*[!0-9]*|0) die "$1 needs a positive whole number (got \"$2\")" ;;
                esac
                JOBS=$2; shift 2 ;;
            -v|--verbose) VERBOSE=1; shift ;;
            -f|--force) FORCE=1; shift ;;
            --rebuild) REBUILD=1; shift ;;
            -*) die "unknown option: $1 (try --help)" ;;
            *)
                if [ -z "$INPUT" ]; then INPUT=$1
                elif [ -z "$REQUEST" ]; then REQUEST=$1
                else die "unexpected argument: \"$1\" (archive and request name only)"
                fi
                shift ;;
        esac
    done
    [ -n "$INPUT" ] || { usage >&2; exit 1; }
    if [ "$TRIAGE" -eq 1 ]; then
        [ "$SURVEY" -eq 1 ] && die "--triage and --survey are different runs: --survey counts converted pages, --triage converts nothing"
        [ "$APPENDIX" -eq 1 ] && die "--appendix-over cannot be combined with --triage (nothing is converted, so nothing moves)"
    fi

    # --- validate input -------------------------------------------------------
    [ -f "$INPUT" ] || [ -d "$INPUT" ] || die "input not found: $INPUT"
    [ -r "$INPUT" ] || die "input file not readable: $INPUT"
    case "$INPUT" in
        *.mbox|*.MBOX|*.pst|*.PST) : ;;
        *) printf '%swarning:%s "%s" does not end in .pst or .mbox, converting anyway\n' \
            "$BOLD" "$RESET" "$INPUT" >&2 ;;
    esac

    IN_DIR="$(cd "$(dirname "$INPUT")" && pwd)"
    IN_NAME="$(basename "$INPUT")"
    [ -n "$REQUEST" ] || REQUEST="Request $(date +%F)"

    # Only the named files are mounted, never their directories, so the
    # container cannot read anything else that happens to sit beside them.
    TERMS_MOUNT=()
    if [ -n "$TERMS" ]; then
        [ -f "$TERMS" ] || die "terms file not found: $TERMS"
        TERMS_DIR="$(cd "$(dirname "$TERMS")" && pwd)"
        TERMS_NAME="$(basename "$TERMS")"
        TERMS_MOUNT=(-v "$TERMS_DIR/$TERMS_NAME":/terms/"$TERMS_NAME":ro)
        PASS+=(--terms "/terms/$TERMS_NAME")
    fi
    if [ -n "$DECISIONS" ]; then
        [ -f "$DECISIONS" ] || die "decisions file not found: $DECISIONS"
        DECISIONS_DIR="$(cd "$(dirname "$DECISIONS")" && pwd)"
        DECISIONS_NAME="$(basename "$DECISIONS")"
        TERMS_MOUNT+=(-v "$DECISIONS_DIR/$DECISIONS_NAME":/decisions/"$DECISIONS_NAME":ro)
        PASS+=(--decisions "/decisions/$DECISIONS_NAME")
    fi

    if [ -n "$SEARCH" ]; then
        # A report folder (Safari unzips the Reports zip) also holds the
        # per-item and per-mailbox reports: only its Summary CSV is mounted.
        if [ -d "$SEARCH" ]; then
            found=()
            for f in "$SEARCH"/[Ss]ummary[-_]*.[Cc][Ss][Vv]; do [ -f "$f" ] && found+=("$f"); done
            [ ${#found[@]} -eq 1 ] || die "expected one Summary-….csv in $SEARCH, found ${#found[@]}
       Pass the folder of the Purview export's report, or its Summary CSV"
            SEARCH=${found[0]}
        fi
        [ -f "$SEARCH" ] || die "search report not found: $SEARCH"
        SEARCH_DIR="$(cd "$(dirname "$SEARCH")" && pwd)"
        SEARCH_NAME="$(basename "$SEARCH")"
        TERMS_MOUNT+=(-v "$SEARCH_DIR/$SEARCH_NAME":/search/"$SEARCH_NAME":ro)
        PASS+=(--search-report "/search/$SEARCH_NAME")
    fi

    if [ -z "$OUTPUT" ]; then
        OUTPUT="$IN_DIR/${IN_NAME%.*}-pdfs"
    fi
    mkdir -p "$OUTPUT" || die "cannot create output directory: $OUTPUT"
    OUT_DIR="$(cd "$OUTPUT" && pwd)"
    if [ -d "$INPUT" ]; then
        INPUT_REAL="$(cd "$INPUT" && pwd)"
        case "$OUT_DIR/" in "$INPUT_REAL/"*) die "output must be outside the MBOX input directory" ;; esac
    fi
    if [ "$FORCE" -eq 0 ] && [ -n "$(ls -A "$OUT_DIR" 2>/dev/null)" ]; then
        die "output directory is not empty: $OUT_DIR
       Re-running into it would mix runs (use --force to allow, or pick another with -o)"
    fi

    # --- find a docker CLI and a running daemon -------------------------------
    DOCKER=""
    # PATH first, then common install locations for shells whose PATH lacks them.
    for candidate in docker /usr/local/bin/docker /opt/homebrew/bin/docker; do
        if command -v "$candidate" >/dev/null 2>&1; then DOCKER=$candidate; break; fi
    done
    [ -n "$DOCKER" ] || die "no docker CLI found
       Install Docker (or any Docker-compatible runtime), then retry"
    "$DOCKER" info >/dev/null 2>&1 || die "Docker is installed but not running
       Start Docker, then retry"

    # --- build the image if needed --------------------------------------------
    # The code runs from the image, not from this folder, so after an update
    # (git pull, or a fresh download) an image built earlier would silently
    # run the old version. The image carries a fingerprint of what it was
    # built from (the pst2pdf.source label), and any difference rebuilds it.
    #
    # The pst2pdf.image-revision label predates the fingerprint and is still
    # checked, for a host with no SHA-256 tool (no fingerprint): revision 1 added
    # tini (without it LibreOffice's orphaned gpg helpers pile up as zombies
    # under PID 1 until --pids-limit is exhausted), revision 2 the CJK font
    # (without it Chinese, Japanese and Korean text is drawn as empty boxes),
    # revision 3 the emoji font (the same for emoji), revision 4 a font cache
    # that stays valid (without it every render process rescans the fonts and
    # some print "Fontconfig error: No writable cache directories"), revision
    # 5 stable readpst meeting .ics attachments (without it their sha256
    # changes on every extraction, so a triage decision matches nothing in
    # the production run), revision 6 the --triage run (an older image
    # refuses the flag), revision 7 --withheld-by and the DOC- Bates prefix
    # (the same), revision 8 --search-report (the same). Images from before
    # the label have none, which reads as 0.
    SOURCE=$(source_fingerprint)
    UPDATED=0
    IMAGE_REVISION=$(sed -n 's/^LABEL pst2pdf.image-revision="\([0-9]*\)".*/\1/p' "$SCRIPT_DIR/Dockerfile")
    if "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
        have=$("$DOCKER" image inspect "$IMAGE" --format '{{index .Config.Labels "pst2pdf.image-revision"}}' 2>/dev/null)
        case "$have" in ''|*[!0-9]*) have=0 ;; esac  # no label: "<no value>"
        [ "$have" -lt "${IMAGE_REVISION:-0}" ] && UPDATED=1
        built_from=$("$DOCKER" image inspect "$IMAGE" --format '{{index .Config.Labels "pst2pdf.source"}}' 2>/dev/null)
        [ -n "$SOURCE" ] && [ "$built_from" != "$SOURCE" ] && UPDATED=1
    fi
    if [ "$UPDATED" -eq 1 ] && [ "$REBUILD" -eq 0 ]; then
        step "pst2pdf has changed since its Docker image was built: rebuilding the image (takes a few minutes)"
        REBUILD=1
    elif [ "$REBUILD" -eq 1 ] || ! "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
        step "Building the $IMAGE Docker image (takes a few minutes)"
        REBUILD=1
    fi
    if [ "$REBUILD" -eq 1 ]; then
        BUILD_CA=()
        if [ -n "${PST2PDF_BUILD_CA:-}" ]; then
            [ -r "$PST2PDF_BUILD_CA" ] || die "PST2PDF_BUILD_CA must name a readable CA bundle"
            BUILD_CA=(--secret "id=proxy_ca,src=$PST2PDF_BUILD_CA")
        fi
        "$DOCKER" build ${BUILD_CA[@]+"${BUILD_CA[@]}"} ${SOURCE:+--label "pst2pdf.source=$SOURCE"} -t "$IMAGE" "$SCRIPT_DIR" \
            || die "Docker image build failed"
    fi

    # --- convert --------------------------------------------------------------
    if [ "$SURVEY" -eq 1 ]; then
        step "Surveying \"$IN_NAME\" (page counts only, no volumes)"
    elif [ "$TRIAGE" -eq 1 ]; then
        step "Preparing a triage copy of \"$IN_NAME\" (attachments as original files, nothing converted)"
    else
        step "Converting \"$IN_NAME\""
    fi
    printf '%s    output: %s%s\n' "$DIM" "$OUT_DIR" "$RESET"
    SECONDS=0

    # Dates on the START HERE and index pages should read in local time.
    TZ_NAME="${TZ:-$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')}"
    [ -n "$TZ_NAME" ] || TZ_NAME=UTC

    # The archive is untrusted input: no network, no capabilities, no privilege
    # escalation, a process cap (LibreOffice and the render workers together
    # stay well under it, now that the image's tini reaps LibreOffice's orphans
    # instead of letting them hold pids as zombies), and only the archive file
    # itself mounted read-only.
    run_container() {
        "$DOCKER" run --rm --network none \
            --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 \
            --user "$(id -u):$(id -g)" -e HOME=/tmp -e TZ="$TZ_NAME" \
            -v "$IN_DIR/$IN_NAME":/in/"$IN_NAME":ro -v "$OUT_DIR":/out \
            ${TERMS_MOUNT[@]+"${TERMS_MOUNT[@]}"} \
            "$IMAGE" -v ${JOBS:+--jobs "$JOBS"} --request-id "$REQUEST" \
            ${PASS[@]+"${PASS[@]}"} "/in/$IN_NAME" /out
    }

    if [ "$VERBOSE" -eq 1 ]; then
        run_container
        STATUS=$?
    else
        # The container logs one "INFO wrote ..." line per message; fold those
        # into a live counter and hide routine INFO/WARNING noise.
        run_container 2>&1 | {
            count=0
            while IFS= read -r line; do
                case "$line" in
                    "INFO wrote "*)
                        count=$((count + 1))
                        [ -t 1 ] && printf '\r    %d messages rendered...' "$count"
                        ;;
                    "INFO stage: "*)
                        if [ "$count" -gt 0 ]; then printf '\n'; count=0; fi
                        printf '    %s\n' "${line#INFO stage: }"
                        ;;
                    INFO*|WARNING*) ;;
                    *)
                        if [ "$count" -gt 0 ]; then printf '\n'; count=-1; fi
                        # The container sees the output directory as /out.
                        printf '%s\n' "${line//\/out\//$OUT_DIR/}"
                        ;;
                esac
            done
            if [ "$count" -gt 0 ]; then printf '\n'; fi
        }
        STATUS=${PIPESTATUS[0]}
    fi

    # --- wrap up --------------------------------------------------------------
    ELAPSED="$((SECONDS / 60))m $((SECONDS % 60))s"
    # The original attachments are unredacted. A triage run is all kept
    # back and goes to the reviewers whole, so its zip says so in its name; any other
    # run's zip leaves the originals out.
    NATIVES_DIR="Original attachments - DO NOT RELEASE"
    if [ "$STATUS" -eq 0 ] && [ "$ZIP" -eq 1 ]; then
        OUT_BASE="$(basename "$OUT_DIR")"
        if [ "$TRIAGE" -eq 1 ] || [ "$SURVEY" -eq 1 ]; then
            ZIP_PATH="$OUT_DIR - triage - DO NOT RELEASE.zip"
            [ "$SURVEY" -eq 0 ] || ZIP_PATH="$OUT_DIR - survey - DO NOT RELEASE.zip"
            rm -f "$ZIP_PATH"
            (cd "$(dirname "$OUT_DIR")" && zip -qr "$ZIP_PATH" "$OUT_BASE") \
                || die "zip failed (output is still in $OUT_DIR)"
        else
            ZIP_PATH="$OUT_DIR.zip"
            rm -f "$ZIP_PATH"
            (cd "$(dirname "$OUT_DIR")" && zip -qr "$ZIP_PATH" "$OUT_BASE" -x "$OUT_BASE/$NATIVES_DIR/*" "$OUT_BASE/manifest.json" "$OUT_BASE/*DO NOT RELEASE*" "$OUT_BASE/00 START HERE.pdf" "$OUT_BASE/Redaction word list.txt" "$OUT_BASE/Attachment survey.*") \
                || die "zip failed (output is still in $OUT_DIR)"
        fi
        printf '    zip: %s\n' "$ZIP_PATH"
        if [ "$TRIAGE" -eq 0 ] && [ -d "$OUT_DIR/$NATIVES_DIR" ]; then
            printf '    The folder "%s" was left out of the zip: it is not redacted.\n' "$NATIVES_DIR"
        fi
    fi
    if [ "$STATUS" -eq 0 ] && [ "$TRIAGE" -eq 1 ]; then
        printf '%s==> Triage done%s in %s. Send for review: %s\n' "$GREEN" "$RESET" "$ELAPSED" "$OUT_DIR"
        printf '    Reviewers fill in: %s\n' "$OUT_DIR/Attachment index - DO NOT RELEASE.csv"
        printf '    Nothing in it is for release. Run again with --decisions and that file to build the volumes.\n'
    elif [ "$STATUS" -eq 0 ] && [ "$SURVEY" -eq 1 ]; then
        printf '%s==> Survey done%s in %s. Read: %s\n' "$GREEN" "$RESET" "$ELAPSED" "$OUT_DIR/Attachment survey.pdf"
        printf '    No volumes were made. Run again without --survey to build them.\n'
    elif [ "$STATUS" -eq 0 ]; then
        printf '%s==> Done%s in %s. Output is in: %s\n' "$GREEN" "$RESET" "$ELAPSED" "$OUT_DIR"
    else
        printf '%s==> Finished with errors%s (exit %d) after %s. Partial output: %s\n' \
            "$RED" "$RESET" "$STATUS" "$ELAPSED" "$OUT_DIR" >&2
    fi
    exit "$STATUS"
}

main "$@"; exit  # one line: nothing after it is ever read
