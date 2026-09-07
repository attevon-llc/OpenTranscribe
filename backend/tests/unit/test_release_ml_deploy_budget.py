"""One OpenSearch-ML readiness budget for all three rehearsal scenarios.

WHAT THE THREE SCENARIOS DISAGREED ABOUT

`test-fresh-install.sh` and `test-lite-mode.sh` polled for **600 s**; `test-upgrade.sh` polled
for **180 s**, on three byte-alike copies of the same loop.

The 180 had a defence, but it lived in a DIFFERENT FILE — `test-fresh-install.sh`'s own budget
comment read "Unlike test-upgrade.sh (180s poll, warmer stack)". MEASURED 2026-09-07 on this
host: the premise is false. The shared rehearsal cache's `opensearch-ml` directory is EMPTY
(4 KB), and `test-upgrade.sh`'s own `mc_seed_cache` call explicitly omits `opensearch-ml` as
"container-specific" — so the upgrade stack registers the neural model over the network from
cold exactly like the other two. There is no warmer stack; there was a number nobody
re-derived, guarded by a comment about a different script.

Consequence if it fires: `as_assert_ge "OpenSearch ML model deployed post-upgrade" 0 1` FAILs,
and a rehearsal reports a neural-search regression that is really a timeout. Hybrid search
passes anyway via BM25 fallback, so the finding reads as specific and real.

The number that IS derived: `ml_model_service._REGISTRATION_MAX_WAIT` is 300 s, and
`test-fresh-install.sh` recorded that even 300 was not always enough on this host under
concurrent build/scan load. 600 costs nothing on a healthy run — the poll exits on the first
successful probe.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RT_DIR = REPO_ROOT / "scripts" / "release-tests"
API_CLIENT = RT_DIR / "lib" / "api-client.sh"
SCENARIOS = ["test-upgrade.sh", "test-fresh-install.sh", "test-lite-mode.sh"]

pytestmark = pytest.mark.skipif(
    not API_CLIENT.exists(), reason="release-test api-client lib not in this checkout"
)


def _run_helper(tmp_path: Path, *, deployed: str, timeout: int = 1) -> tuple[int, str, str]:
    """Run the REAL ac_wait_for_ml_model_deployed against a fake `docker`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{tmp_path}/docker.log"\n'
        f'printf \'{{"hits":{{"total":{{"value":{deployed}}}}}}}\'\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    # Invoked exactly the way the three scenarios invoke it: captured, with `|| rc=$?`.
    # api-client.sh sets `set -euo pipefail` at source time, so a BARE call whose return is
    # non-zero would kill the calling script — which is the #617/#618 truncation shape and
    # the reason every call site guards it.
    snippet = f"""
set -uo pipefail
API_BASE="http://localhost:5174/api"
ML_DEPLOY_POLL_S=0
source "{API_CLIENT}"
rc=0
deployed=$(ac_wait_for_ml_model_deployed {timeout}) || rc=$?
echo "DEPLOYED=$deployed"
echo "RC=$rc"
echo "STILL_ALIVE=yes"
"""
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)
    log = (
        (tmp_path / "docker.log").read_text(encoding="utf-8")
        if (tmp_path / "docker.log").exists()
        else ""
    )
    return proc.returncode, proc.stdout, log


@pytest.mark.unit
def test_the_budget_is_600_and_lives_in_exactly_one_place() -> None:
    text = API_CLIENT.read_text(encoding="utf-8")

    assert 'ML_DEPLOY_TIMEOUT_S="${ML_DEPLOY_TIMEOUT_S:-600}"' in text, (
        "the ML-deploy budget must be a single named, overridable default of 600"
    )


@pytest.mark.unit
@pytest.mark.parametrize("name", SCENARIOS)
def test_every_scenario_uses_the_shared_helper(name: str) -> None:
    path = RT_DIR / name
    if not path.exists():
        pytest.skip(f"{name} not present in this checkout")
    text = path.read_text(encoding="utf-8")

    assert "ac_wait_for_ml_model_deployed" in text, (
        f"{name} must poll ML readiness through the shared helper, not its own loop"
    )
    guarded = [
        line
        for line in text.splitlines()
        if "ac_wait_for_ml_model_deployed" in line and "$(" in line
    ]
    assert guarded and all("||" in line for line in guarded), (
        f"{name} must guard the captured call — api-client.sh sources with `set -e`, so a "
        f"bare non-zero return truncates every phase after it: {guarded}"
    )
    assert "_plugins/_ml/models/_search" not in text, (
        f"{name} still carries its own inline ML-readiness poll — that is how the budgets "
        f"drifted to 600/600/180 in the first place"
    )


@pytest.mark.unit
def test_no_scenario_still_carries_a_180_second_ml_wait() -> None:
    """The specific drifted literal, named, so a revert is visible."""
    for name in SCENARIOS:
        path = RT_DIR / name
        if not path.exists():
            continue
        assert not re.search(r'ml_wait"? -lt 180', path.read_text(encoding="utf-8")), name


@pytest.mark.unit
def test_helper_reports_the_deployed_count_and_succeeds(tmp_path: Path) -> None:
    rc, out, log = _run_helper(tmp_path, deployed="1")

    assert "RC=0" in out, out
    assert "DEPLOYED=1" in out, f"the count must be echoed for the caller's as_assert_ge: {out!r}"
    assert "exec opentranscribe-opensearch" in log, (
        f"the poll must run inside the OpenSearch container: {log!r}"
    )


@pytest.mark.unit
def test_helper_returns_zero_and_nonzero_on_timeout_rather_than_dying(tmp_path: Path) -> None:
    """It must not be fatal.

    A bare non-zero from a helper under this harness's `set -e` truncates every phase after
    it with no error trace — the #617/#618 shape. Neural search failing to deploy is a finding
    the scenario RECORDS via as_assert_ge, and the phases after it still have to run.
    """
    rc, out, _ = _run_helper(tmp_path, deployed="0")

    assert "RC=1" in out, f"a timeout must report failure to the caller: {out!r}"
    assert "DEPLOYED=0" in out, (
        f"the count must still be echoed so as_assert_ge records a FAIL: {out!r}"
    )
    assert "STILL_ALIVE=yes" in out, (
        "the caller must reach the phases after this one — a timeout is a finding to RECORD, "
        "not a reason to truncate the rehearsal (issues #617/#618)"
    )
    assert rc == 0, out
