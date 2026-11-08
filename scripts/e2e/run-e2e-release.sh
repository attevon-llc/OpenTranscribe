#!/bin/bash
# OpenTranscribe — release-gate E2E selection: the v0.6.0 browser journeys (issue #1282 item A).
#
# run-e2e-smoke.sh (5 files, read-mostly) is the everyday check. A release additionally has to
# prove the journeys v0.6.0 shipped, and until now no release stage ran them. Wraps run-e2e.sh,
# so --fresh <name> / E2E_FRONTEND_URL / E2E_BACKEND_URL work the same way. The release `test`
# stage (scripts/release/60-test.sh) calls this against its own isolated fresh stack.
#
# Every file below must exist; backend/tests/unit/test_release_e2e_selection.py fails otherwise,
# so a rename cannot silently turn this selection into "0 tests collected".

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$SCRIPT_DIR/run-e2e.sh" \
    backend/tests/e2e/test_about_dialog.py \
    backend/tests/e2e/test_export_downloads.py \
    backend/tests/e2e/test_media_playback.py \
    backend/tests/e2e/test_nav_menus_and_autocomplete.py \
    backend/tests/e2e/test_rtl_privacy.py \
    backend/tests/e2e/test_upload_tray_and_notifications.py \
    backend/tests/e2e/test_notification_missed_frames.py \
    backend/tests/e2e/test_privacy_settings.py \
    backend/tests/e2e/test_privacy_switch_keyboard.py \
    backend/tests/e2e/test_privacy_voiceprint_hiding.py \
    "$@"
