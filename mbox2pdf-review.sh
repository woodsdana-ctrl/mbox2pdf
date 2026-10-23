#!/usr/bin/env bash
# Local review UI, with a separate network-isolated conversion worker.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p review-data
chmod 700 review-data
export REVIEW_UID="$(id -u)" REVIEW_GID="$(id -g)"
BUILD_CA=()
if [ -n "${PST2PDF_BUILD_CA:-}" ]; then
    BUILD_CA=(--secret "id=proxy_ca,src=$PST2PDF_BUILD_CA")
fi
docker build "${BUILD_CA[@]}" -t mbox2pdf-review .
printf '\nOpen http://127.0.0.1:8765 in your browser. Keep this window open.\nStop with Ctrl+C; your review decisions stay saved in review-data/.\n'
exec docker compose up --no-build
