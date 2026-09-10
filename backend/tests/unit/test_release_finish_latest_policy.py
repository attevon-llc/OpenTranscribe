"""`95-finish.sh` must not mark a BACKPORT as GitHub "Latest".

THE BUG

`gh release create ... --latest` was passed unconditionally. `90-promote.sh` already refuses
to move Docker `:latest` backwards when a hotfix is cut from an old `release/<minor>` branch
(issue #784) — it resolves `newest_published_release()` and skips the digest copy. `finish`
had no such guard, so the SAME backport would still move the GitHub "Latest" pointer
backwards. That pointer is what `setup-opentranscribe.sh` resolves for a `curl | bash`
install, so the guard held for the images and leaked at the installer: every new user would
have been handed the hotfix instead of the newer minor.

WHY `--latest=false` AND NOT AN OMITTED FLAG

Omitting `--latest` does not mean "not latest". `gh release create --help` documents the
default as "automatic based on date and version" — GitHub's own heuristic. Replacing a
deliberate wrong answer with a guess is not a fix; the policy has to be stated.

APPROACH

The `# --- BEGIN latest-policy ---` block is extracted from the real `95-finish.sh` and run
with stub `newest_published_release`/`ver_lt`/`record`/`fail_out`, so the decision logic under
test is the shipped one. A separate check reads the real `gh release create` invocation and
asserts it interpolates the derived variable rather than a literal flag — otherwise the block
could compute the right answer and the command could ignore it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FINISH_SH = REPO_ROOT / "scripts" / "release" / "95-finish.sh"

pytestmark = pytest.mark.skipif(
    not FINISH_SH.exists(), reason="scripts/release/95-finish.sh not present in this checkout"
)


def _extract_marked_block(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    start = text.index(f"# --- BEGIN {name} ---")
    end = text.index(f"# --- END {name} ---", start)
    return text[start : text.index("\n", end) + 1]


def _run_policy(
    tmp_path: Path, *, version: str, newest: str | None
) -> tuple[int, str, str, list[list[str]]]:
    """Run the REAL latest-policy block. Returns (rc, latest_flag, output, record rows).

    The block sources `$SCRIPT_DIR/patch-lib.sh`, so `SCRIPT_DIR` is pointed at a scratch
    directory holding a stand-in. That stand-in sources the REAL `versions.sh` — `ver_lt` is
    the comparison under test and must not be faked — and replaces only
    `newest_published_release`, whose real body needs git tags AND live Docker Hub lookups.
    """
    record_log = tmp_path / "records.log"
    script_dir = tmp_path / "release"
    script_dir.mkdir()
    resolver = (
        f'newest_published_release() {{ printf %s "{newest}"; return 0; }}'
        if newest is not None
        else "newest_published_release() { return 1; }"
    )
    (script_dir / "patch-lib.sh").write_text(
        "#!/bin/bash\n"
        f'REPO_ROOT="{REPO_ROOT}"\n'
        f'source "{REPO_ROOT}/scripts/release-tests/lib/versions.sh"\n'
        "set +e\n"
        f"{resolver}\n",
        encoding="utf-8",
    )
    snippet = f"""
set -uo pipefail
RED=''; YELLOW=''; NC=''
SCRIPT_DIR="{script_dir}"
VERSION="{version}"
record() {{ printf '%s\\t%s\\t%s\\n' "$1" "${{2:-}}" "${{3:-}}" >> "{record_log}"; }}
fail_out() {{ echo "FAIL_OUT=$1"; exit "$1"; }}

{_extract_marked_block(FINISH_SH, "latest-policy")}

echo "LATEST_FLAG=$latest_flag"
"""
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    flag_match = re.search(r"^LATEST_FLAG=(.*)$", proc.stdout, re.M)
    records = [
        line.split("\t")
        for line in (
            record_log.read_text(encoding="utf-8").splitlines() if record_log.exists() else []
        )
        if line.strip()
    ]
    return proc.returncode, (flag_match.group(1) if flag_match else ""), out, records


@pytest.mark.unit
def test_a_backport_is_not_marked_latest(tmp_path: Path) -> None:
    """v0.4.2 cut while v0.5.0 is already published — the #784 hotfix shape."""
    rc, flag, out, records = _run_policy(tmp_path, version="v0.4.2", newest="v0.5.0")

    assert rc == 0, f"a backport is a legitimate release, not a refusal:\n{out}"
    assert flag == "--latest=false", (
        f"a backport must explicitly NOT be marked Latest — the installer resolves that "
        f"pointer and would downgrade every new install; got {flag!r}"
    )
    outcomes = {r[0]: r[1] for r in records}
    assert outcomes.get("github-latest-policy") == "pass", records


@pytest.mark.unit
def test_the_newest_release_is_marked_latest(tmp_path: Path) -> None:
    """Must-stay-clean control: the ordinary release must still become Latest.

    Without this, a policy that returned `--latest=false` for everything would look
    identical to a working one on the backport case alone.
    """
    rc, flag, out, records = _run_policy(tmp_path, version="v0.5.0", newest="v0.5.0")

    assert rc == 0, out
    assert flag == "--latest", f"the release being published must be marked Latest; got {flag!r}"
    outcomes = {r[0]: r[1] for r in records}
    assert outcomes.get("github-latest-policy") == "pass", records


@pytest.mark.unit
def test_a_newer_version_than_anything_published_is_marked_latest(tmp_path: Path) -> None:
    rc, flag, _, _ = _run_policy(tmp_path, version="v0.6.0", newest="v0.5.0")

    assert rc == 0
    assert flag == "--latest"


@pytest.mark.unit
def test_an_unresolvable_newest_release_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """ "I don't know whether this is a downgrade" is not a licence to move the pointer.

    Same rule 90-promote.sh applies to the identical question: `newest_published_release`
    returning 1 means Docker Hub / git could not be consulted, never "nothing is published".
    """
    rc, _, out, records = _run_policy(tmp_path, version="v0.5.0", newest=None)

    assert "FAIL_OUT=1" in out, f"an unresolved newest release must refuse the stage:\n{out}"
    assert rc == 1, out
    policy = [r for r in records if r[0] == "github-latest-policy"]
    assert policy and policy[0][1] == "not-measured", (
        f"could-not-check is not-measured, never a pass or a fail:\n{records}"
    )


@pytest.mark.unit
def test_gh_release_create_uses_the_derived_flag() -> None:
    """The wiring: computing the right answer and then hardcoding `--latest` fixes nothing."""
    text = FINISH_SH.read_text(encoding="utf-8")
    start = text.index("gh release create")
    invocation = text[start : text.index('"${assets[@]}"', start)]

    assert '"$latest_flag"' in invocation, (
        f"gh release create must pass the derived flag:\n{invocation}"
    )
    assert not re.search(r"^\s*--latest\b", invocation, re.M), (
        f"a literal --latest in the invocation overrides the policy above it:\n{invocation}"
    )
