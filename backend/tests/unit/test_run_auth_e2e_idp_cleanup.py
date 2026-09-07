"""`scripts/run-auth-e2e.sh` must never remove an IdP overlay it did not start.

Phase 2 (`phase_2_ldap_keycloak`) can bring up LLDAP and/or Keycloak, whichever of the two is
not already running. The script's own stated invariant (a comment a few lines above the
cleanup block) is that `--cleanup` "never removes an IdP the operator had running beforehand".

That invariant was broken by a single shared `IDP_OVERLAYS_STARTED` boolean: if only ONE of
the two IdPs was actually started by a given run (the other was already up), the boolean still
flipped `true`, and `cleanup_on_exit`'s `for idp_svc in lldap keycloak` loop stopped and
removed BOTH — including the one the operator had running before the script ever ran.

Fixed by tracking each service's "did THIS run start it" state independently
(`LLDAP_OVERLAY_STARTED` / `KEYCLOAK_OVERLAY_STARTED`), consulted individually inside the
cleanup loop via a per-service lookup rather than one shared flag guarding the whole loop.

These tests extract three self-contained fragments the fix touches (marked with `# BEGIN
idp-*` / `# END idp-*` sentinels in the script) and drive them directly with stubbed
`port_open`/`docker` calls — no live containers, no `opentr.sh`, no network — proving the
state-tracking logic in isolation, the same technique `test_install_upgrade_scripts.py` and
`test_release_ledger_abort.py` use for shell logic in this repo.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "run-auth-e2e.sh"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(), reason="scripts/run-auth-e2e.sh not present in this checkout"
)


def _extract_between(text: str, begin: str, end: str) -> str:
    match = re.search(rf"# BEGIN {re.escape(begin)}\n(.*?)\n\s*# END {re.escape(end)}", text, re.S)
    assert match, f"sentinel block {begin!r}..{end!r} not found in {SCRIPT.name}"
    return match.group(1)


def _decide_and_mark(text: str, lldap_open: bool, keycloak_open: bool) -> dict[str, str]:
    """Run the flag-decision + mark-started fragments for one port-availability scenario.

    Mirrors what phase_2_ldap_keycloak does before it ever touches opentr.sh/pytest: decide
    which overlays are missing, then (simulating a successful `opentr.sh start dev` for
    whichever flags were collected) mark only those as started by this run.
    """
    start_flags_block = _extract_between(text, "idp-start-flags", "idp-start-flags")
    mark_started_block = _extract_between(text, "idp-mark-started", "idp-mark-started")

    # Wrapped in a function because the extracted fragment uses `local` (as it does in
    # phase_2_ldap_keycloak), which bash only permits inside a function body.
    snippet = f"""
set -euo pipefail
log_ok()   {{ :; }}
port_open() {{
    case "$1" in
        3890) {"return 0" if lldap_open else "return 1"} ;;
        8180) {"return 0" if keycloak_open else "return 1"} ;;
    esac
}}
LLDAP_OVERLAY_STARTED=false
KEYCLOAK_OVERLAY_STARTED=false

_decide_and_mark() {{
{start_flags_block}

if [ ${{#idp_flags[@]}} -gt 0 ]; then
{mark_started_block}
fi

echo "FLAGS=${{idp_flags[*]:-}}"
}}
_decide_and_mark
echo "LLDAP_OVERLAY_STARTED=$LLDAP_OVERLAY_STARTED"
echo "KEYCLOAK_OVERLAY_STARTED=$KEYCLOAK_OVERLAY_STARTED"
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=True,
    )
    out = proc.stdout
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in out.strip().splitlines()
        if "=" in line
    }


def _run_cleanup_loop(
    text: str, do_cleanup: bool, lldap_started: bool, keycloak_started: bool
) -> list[str]:
    """Run the cleanup-loop fragment and return which containers were `docker stop`/`rm`'d."""
    cleanup_block = _extract_between(text, "idp-cleanup-loop", "idp-cleanup-loop")

    snippet = f"""
set -euo pipefail
log_step() {{ :; }}
log_ok()   {{ :; }}
overlay_container_name() {{ echo "opentranscribe-$1-test"; }}
STOPPED_LOG="$(mktemp)"
docker() {{
    if [ "$1" = "stop" ] || [ "$1" = "rm" ]; then
        echo "$1:$2" >> "$STOPPED_LOG"
    fi
    return 0
}}
DO_CLEANUP={"true" if do_cleanup else "false"}
LLDAP_OVERLAY_STARTED={"true" if lldap_started else "false"}
KEYCLOAK_OVERLAY_STARTED={"true" if keycloak_started else "false"}

_cleanup() {{
{cleanup_block}
}}
_cleanup

cat "$STOPPED_LOG"
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=True,
    )
    return [line for line in proc.stdout.strip().splitlines() if line]


TEXT = SCRIPT.read_text() if SCRIPT.exists() else ""


@pytest.mark.unit
def test_only_missing_overlay_is_marked_started_when_one_already_runs() -> None:
    """LLDAP already up, Keycloak missing -> only Keycloak should be marked as started."""
    result = _decide_and_mark(TEXT, lldap_open=True, keycloak_open=False)
    assert result["FLAGS"] == "--with-keycloak-test"
    assert result["LLDAP_OVERLAY_STARTED"] == "false"
    assert result["KEYCLOAK_OVERLAY_STARTED"] == "true"


@pytest.mark.unit
def test_both_marked_started_when_neither_already_runs() -> None:
    result = _decide_and_mark(TEXT, lldap_open=False, keycloak_open=False)
    assert result["LLDAP_OVERLAY_STARTED"] == "true"
    assert result["KEYCLOAK_OVERLAY_STARTED"] == "true"


@pytest.mark.unit
def test_neither_marked_started_when_both_already_run() -> None:
    result = _decide_and_mark(TEXT, lldap_open=True, keycloak_open=True)
    assert result["FLAGS"] == ""
    assert result["LLDAP_OVERLAY_STARTED"] == "false"
    assert result["KEYCLOAK_OVERLAY_STARTED"] == "false"


@pytest.mark.unit
def test_cleanup_only_removes_the_overlay_this_run_started() -> None:
    """The exact bug: this run started only Keycloak (LLDAP was already up before it ran).

    `--cleanup` must remove Keycloak's container and must NOT touch LLDAP's — the operator's
    pre-existing LLDAP must survive.
    """
    stopped = _run_cleanup_loop(TEXT, do_cleanup=True, lldap_started=False, keycloak_started=True)
    assert any("opentranscribe-keycloak-test" in line for line in stopped), stopped
    assert not any("opentranscribe-lldap-test" in line for line in stopped), (
        f"cleanup removed an IdP this run did not start:\n{stopped}"
    )


@pytest.mark.unit
def test_cleanup_removes_both_when_both_were_started() -> None:
    stopped = _run_cleanup_loop(TEXT, do_cleanup=True, lldap_started=True, keycloak_started=True)
    assert any("opentranscribe-lldap-test" in line for line in stopped), stopped
    assert any("opentranscribe-keycloak-test" in line for line in stopped), stopped


@pytest.mark.unit
def test_cleanup_removes_nothing_when_this_run_started_neither() -> None:
    """Both IdPs were already running before this script ran — cleanup must leave both alone."""
    stopped = _run_cleanup_loop(TEXT, do_cleanup=True, lldap_started=False, keycloak_started=False)
    assert stopped == [], f"cleanup touched a container it never started:\n{stopped}"
