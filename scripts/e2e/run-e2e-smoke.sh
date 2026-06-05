#!/bin/bash
# OpenTranscribe — quick E2E smoke pass (read-mostly subset, ~3 minutes).
# Wraps run-e2e.sh with the fast, side-effect-free test files.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$SCRIPT_DIR/run-e2e.sh" \
    backend/tests/e2e/test_settings_modal.py \
    backend/tests/e2e/test_a11y.py \
    backend/tests/e2e/test_file_detail_transcript.py \
    backend/tests/e2e/test_media_download.py \
    "$@"
