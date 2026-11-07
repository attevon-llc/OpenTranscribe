#!/bin/bash
# OpenTranscribe — first-run wizard terms-step E2E (issue #1366).
#
# The wizard only opens for a super_admin whose stack has never completed it, and the test
# accepts the operator terms, so it consumes that state. It therefore runs on a FRESH stack,
# FIRST, before anything else dismisses the wizard (complete_first_run_wizard / the
# first_run_wizard_completed fixture), and run-e2e.sh excludes the `first_run` marker from its
# phases. Do NOT point it at a stack whose data matters.
#
#   ./opentr.sh start dev --fresh wiz --port-offset 600
#   ./scripts/e2e/run-e2e-first-run.sh --fresh wiz
#
# Passes `-m first_run` through run-e2e.sh's single-pytest path (an explicit -m wins).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$SCRIPT_DIR/run-e2e.sh" \
    backend/tests/e2e/test_first_run_wizard_terms.py \
    -m first_run \
    "$@"
