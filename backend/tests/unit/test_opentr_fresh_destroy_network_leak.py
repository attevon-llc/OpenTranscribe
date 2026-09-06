"""`fresh-destroy` must reclaim the project's network, or say loudly that it could not.

Issue #772 — the third leak in `fresh_destroy`, after #767 closed profile-gated services
and project-scoped image tags. Volumes and image tags each had an explicit reclaim step;
the network had none. It got only whatever `docker compose down` managed, and `down` was
wrapped in ``2>/dev/null || true``, so a network it failed to remove was indistinguishable
from one it removed.

Unlike a leaked container, nothing later makes this visible. **Eleven accumulated on the
development host**, each holding a ``/16`` out of Docker's default pool (172.17-172.31),
until ``docker network create`` began failing HOST-WIDE with "all predefined address pools
have been fully subnetted" — blocking unrelated projects and a release task, with nothing
pointing back at `fresh-destroy`.

⚠️ Removal can genuinely fail for a cause this repo cannot fix: Docker reports
``has active endpoints`` while the network shows ``containers=0`` and nothing is attached.
That is a stale endpoint record; it survives teardown and only a daemon restart clears it
(``docker network prune`` uses the same path and fails identically). So these tests do NOT
assert that removal succeeds. They assert the two things that were actually wrong: that
removal is **attempted**, and that a failure is **reported** rather than swallowed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"


def _fresh_destroy_body() -> str:
    src = OPENTR.read_text(encoding="utf-8")
    match = re.search(r"^fresh_destroy\(\) \{$", src, re.M)
    assert match, "fresh_destroy() not found in opentr.sh — renamed?"
    end = re.search(r"^\}$", src[match.start() :], re.M)
    assert end, "no closing brace for fresh_destroy()"
    return src[match.start() : match.start() + end.end()]


def test_the_down_exit_status_is_not_discarded() -> None:
    """`|| true` on the teardown is what made a failed removal look like a success."""
    body = _fresh_destroy_body()
    down = [ln for ln in body.splitlines() if "docker compose $chain down" in ln]
    assert down, "the `docker compose ... down` line is gone — did the teardown move?"
    for line in down:
        assert "|| true" not in line, (
            "`docker compose down` still ends in `|| true`, so a failed teardown is "
            f"reported as a successful one:\n  {line.strip()}"
        )
        assert "_down_rc" in line, (
            f"the down exit status is not captured, so nothing can report it:\n  {line.strip()}"
        )


def test_the_network_removal_is_attempted_explicitly() -> None:
    """Volumes and image tags each get an explicit reclaim; the network needs one too."""
    body = _fresh_destroy_body()
    assert "docker network rm" in body, (
        "fresh_destroy never attempts `docker network rm`. `docker compose down` alone is "
        "what leaked 11 networks — the project network needs the same explicit reclaim "
        "step that volumes and image tags already have."
    )
    assert '_net="${proj}_default"' in body, (
        "the network is not addressed by the project-scoped name `${proj}_default` — an "
        "unscoped removal on this host is how an unrelated container was destroyed before"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("rm_rc", "must_say", "why"),
    [
        (0, "removed leftover network", "a successful reclaim should be stated"),
        (1, "could NOT be removed", "a FAILED reclaim must be loud, not swallowed"),
    ],
)
def test_it_reports_the_outcome_of_the_network_removal(tmp_path, rm_rc, must_say, why) -> None:
    """Run the real fragment against a fake `docker` and read what it actually prints."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # `network inspect` succeeds (the network exists); `network rm` returns rm_rc.
    (bindir / "docker").write_text(
        "#!/bin/bash\n"
        'case "$1 $2" in\n'
        '  "network inspect") exit 0 ;;\n'
        f'  "network rm") exit {rm_rc} ;;\n'
        "esac\n"
        "exit 0\n"
    )
    (bindir / "docker").chmod(0o755)

    body = _fresh_destroy_body()
    start = body.index('local _net="${proj}_default"')
    end = body.index("# Catch any stragglers", start)
    fragment = body[start:end]

    # `local` is only legal inside a function, so the fragment is wrapped in one —
    # running it at top level aborts on line 1 and every assertion below would then be
    # measuring a script that never ran.
    script = (
        "set -uo pipefail\n"
        f'export PATH="{bindir}:$PATH"\n'
        "probe() {\n"
        '  local proj="otfresh-probe" name="probe" _down_rc=0\n'
        f"{fragment}\n"
        "}\n"
        "probe\n"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    combined = proc.stdout + proc.stderr
    assert must_say in combined, f"{why}. Output was:\n{combined}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_failure_message_names_the_host_wide_consequence(tmp_path) -> None:
    """A warning nobody can act on gets ignored; this one has to say what it costs.

    The leak's whole danger is that its cost surfaces far away, as an unrelated
    `docker network create` failure in another project. The message must connect the two.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(
        '#!/bin/bash\ncase "$1 $2" in\n  "network inspect") exit 0 ;;\n'
        '  "network rm") exit 1 ;;\nesac\nexit 0\n'
    )
    (bindir / "docker").chmod(0o755)

    body = _fresh_destroy_body()
    start = body.index('local _net="${proj}_default"')
    end = body.index("# Catch any stragglers", start)

    proc = subprocess.run(
        [
            "bash",
            "-c",
            "set -uo pipefail\n"
            f'export PATH="{bindir}:$PATH"\n'
            "probe() {\n"
            '  local proj="otfresh-probe" name="probe" _down_rc=0\n'
            + body[start:end]
            + "\n}\nprobe\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = proc.stdout + proc.stderr
    for needle, why in [
        ("subnet", "must say the network still holds a subnet"),
        ("host-wide", "must name the host-wide consequence, or nobody acts on it"),
        ("daemon restart", "must say what actually clears a stale-endpoint network"),
        ("prune", "must warn that `docker network prune` will NOT clear it"),
    ]:
        assert needle in out, f"failure message {why}. Output was:\n{out}"
