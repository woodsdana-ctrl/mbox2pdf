#!/usr/bin/env bash
# MBOX front door; reuse the hardened Docker workflow and all review options.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/pst2pdf.sh" --input-format mbox "$@"
