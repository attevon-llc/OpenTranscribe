# shellcheck shell=bash
# The public demo's upgrade record, as the `finish` stage reads it (issue #628, criterion
# `demo-upgraded`). Sourced by 95-finish.sh; no side effects on load.
#
# The record is `demo-status.json`, written on the demo host by scripts/demo/demo-upgrade.sh:
#   {"tag": "<tag the demo now runs>", "previous_tag": "<tag it came from>",
#    "result": "upgraded" | "rolled-back" | "rollback-failed", "at": "<iso8601>"}
# On a rollback `tag` is the OLD tag and `previous_tag` is the one that failed.
#
# Which demo is a property of the operator's environment, not of the repo:
#   DEMO_STATUS_FILE   path to a readable copy of the record
#   DEMO_STATUS_CMD    command printing the record, e.g. `ssh demo cat /opt/opentranscribe/demo-status.json`
#                      (wins over DEMO_STATUS_FILE)
# Neither set means no demo is configured and the criterion is recorded as waived.

demo_status_configured() {
    [[ -n "${DEMO_STATUS_CMD:-}${DEMO_STATUS_FILE:-}" ]]
}

# Print the record. Non-zero when it cannot be read.
demo_status_fetch() {
    if [[ -n "${DEMO_STATUS_CMD:-}" ]]; then
        bash -c "$DEMO_STATUS_CMD"
    else
        cat -- "$DEMO_STATUS_FILE"
    fi
}

# demo_status_verdict VERSION < record
# Prints exactly one of: upgraded | failed | pending | unreadable
#   upgraded   the demo runs VERSION
#   failed     the demo tried VERSION and rolled back (it will not retry on its own)
#   pending    a valid record that does not mention VERSION yet (the timer has not fired)
#   unreadable not JSON, or not the expected shape
demo_status_verdict() {
    python3 -I -c '
import json, sys
version = sys.argv[1]
try:
    data = json.load(sys.stdin)
    tag, previous, result = data["tag"], data.get("previous_tag"), data["result"]
except (ValueError, KeyError, TypeError):
    print("unreadable")
    raise SystemExit(0)
if result == "upgraded" and tag == version:
    print("upgraded")
elif result in ("rolled-back", "rollback-failed") and previous == version:
    print("failed")
else:
    print("pending")
' "$1"
}
