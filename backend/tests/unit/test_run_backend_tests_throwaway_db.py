"""The ``--throwaway-db`` mode of ``scripts/run-backend-tests.sh`` must only ever touch its own container.

The mode starts a Postgres container on a developer machine that also hosts the live dev stack
and unrelated GPU services. The failure that matters is not a flaky run, it is a cleanup that
stops somebody else's container: a name substring or a label filter in a ``docker stop`` would.
So this pins, over the shell source (starting containers to find out is not a unit test):

* every ``docker stop|rm|kill`` names the exact variable holding the container this run created,
* the container carries both the private compose-project label and the purpose label,
* the existence check is anchored (``^name$``), so a prefix match cannot refuse or select others,
* the flag is consumed by the argument pre-pass, so it is never forwarded to pytest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-backend-tests.sh"

#: The only operand a destructive docker verb may take.
EXACT = re.compile(r'^"\$(THROWAWAY_NAME|name)"$')
DESTRUCTIVE = re.compile(
    r"\bdocker\s+(stop|rm|kill|container\s+(?:stop|rm|kill)|compose)\b([^\n|;&]*)"
)


@pytest.fixture(scope="module")
def source() -> str:
    assert SCRIPT.is_file(), f"run-backend-tests.sh not found at {SCRIPT}"
    return SCRIPT.read_text(encoding="utf-8")


def _code_lines(source: str) -> str:
    """The script without comment-only lines, so prose cannot satisfy or trip a check."""
    return "\n".join(ln for ln in source.splitlines() if not ln.lstrip().startswith("#"))


def test_destructive_docker_verbs_take_only_the_exact_container_name(source: str) -> None:
    found = DESTRUCTIVE.findall(_code_lines(source))
    assert found, "no docker stop found: the throwaway container would never be cleaned up"
    for verb, rest in found:
        assert verb == "stop", f"`docker {verb}` is not allowed here; only an exact-name stop"
        operands = [t for t in rest.split() if not t.startswith(">") and not t.startswith("2>")]
        assert len(operands) == 1 and EXACT.match(operands[0]), (
            f"docker stop must take exactly the created container's name, got: {rest.strip()!r}"
        )


def test_cleanup_is_installed_on_exit_and_on_signals(source: str) -> None:
    code = _code_lines(source)
    assert re.search(r"trap\s+cleanup_throwaway\s+EXIT", code), "no EXIT trap stops the container"
    for sig in ("INT", "TERM"):
        assert re.search(rf"trap '[^\n]*cleanup_throwaway[^\n]*' {sig}\b", code), (
            f"the {sig} trap does not stop the container"
        )


def test_container_carries_both_isolation_labels(source: str) -> None:
    run = re.search(r"docker run\b.*?postgres:[\w.\-]+", _code_lines(source), re.S)
    assert run, "docker run for the throwaway database not found"
    block = run.group(0)
    assert re.search(r'--label\s+"com\.docker\.compose\.project=\$\{THROWAWAY_CANDIDATE\}"', block)
    assert '--label "org.opentranscribe.purpose=throwaway-test-db"' in block
    assert re.search(r'--name\s+"\$THROWAWAY_CANDIDATE"', block)
    assert "--tmpfs /var/lib/postgresql/data" in block
    assert re.search(r"-p\s+\"127\.0\.0\.1:", block), "the database must publish on loopback only"


def test_existing_container_check_is_anchored_exact_name(source: str) -> None:
    check = re.search(r'docker ps -a --filter "name=([^"]*)"', _code_lines(source))
    assert check, "no pre-existing-container check"
    assert check.group(1).startswith("^") and check.group(1).endswith("$"), (
        f"name filter must be anchored, got {check.group(1)!r}"
    )


def test_flag_is_consumed_by_the_prepass_not_forwarded_to_pytest(source: str) -> None:
    prepass = source.split("_ARGV=()", 1)[1].split("set -- ", 1)[0]
    assert re.search(r"--throwaway-db\)\s*THROWAWAY_DB=true;\s*shift", prepass)
    # The re-report modes run before any container is started.
    assert source.index("--summary)  report_summary") < source.index("start_throwaway_db ||")


def test_container_name_suffix_is_random_not_derived_from_the_checkout(source: str) -> None:
    code = _code_lines(source)
    start = code.index("start_throwaway_db() {")
    body = code[start : code.index("\nif $THROWAWAY_DB", start)]
    assert re.search(r'slug="\$\(openssl rand -hex \d+\)"', body)
    slug_line = re.search(r"slug=.*", body)
    assert slug_line is not None, "the slug assignment must exist"
    for derived in ("basename", "REPO_ROOT", "PWD"):
        assert derived not in slug_line.group(0)
    assert "platform_args[@]" in body, "the postgres image platform must be pinned"
