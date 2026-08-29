"""Every ``--with-*`` overlay opentr.sh dispatches must be handled or explicitly exempt in
``scripts/run-dev-tests.sh`` (issue #630).

``run-dev-tests.sh`` auto-detects and starts a handful of test overlays (mock-llm,
keycloak-test, ldap-test, and — under ``--all-overlays`` — watch/mock-asr) so a green
``--full`` run is actually exercising what it claims to, rather than silently skipping
whole test classes because a container wasn't up. That table is a hand-maintained dispatch
in the script, and issue #630 exists because a prior, narrower version of this idea
(``--with-llm-test`` missing from ``opentr.sh``'s own ``--fresh`` isolation dispatch) went
unnoticed for the same reason: nothing enumerated the flags.

This is the same convention ``test_opentr_fresh_aux_isolation.py`` uses for ``--fresh``
isolation, applied to a different dispatch: every ``--with-*`` flag ``opentr.sh`` actually
implements must appear either as a key of ``run-dev-tests.sh``'s ``OVERLAY_SERVICE`` table
(managed) or in its ``EXEMPT_WITH_FLAGS`` map (deliberately not managed, with a written
reason) — never neither.

Static, over the shell source: starting a dozen overlay combinations to find out is not a
test anyone would run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

OPENTR = Path(__file__).resolve().parents[3] / "opentr.sh"
# The overlay table (OVERLAY_SERVICE / EXEMPT_WITH_FLAGS) lives in the sourced lib file, not
# run-dev-tests.sh itself — split out to keep that script under this repo's ~300-line
# convention (root CLAUDE.md). This test reads the lib file, which is where the table
# actually is; run-dev-tests.sh only `source`s it.
RUN_DEV_TESTS = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "dev-test-overlays.sh"


@pytest.fixture(scope="module")
def opentr_source() -> str:
    assert OPENTR.is_file(), f"opentr.sh not found at {OPENTR}"
    return OPENTR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run_dev_tests_source() -> str:
    assert RUN_DEV_TESTS.is_file(), f"scripts/lib/dev-test-overlays.sh not found at {RUN_DEV_TESTS}"
    return RUN_DEV_TESTS.read_text(encoding="utf-8")


def _opentr_with_flags(source: str) -> set[str]:
    """Every ``--with-<flag>`` literal opentr.sh's usage/dispatch mentions.

    Matches the flag SPELLING (``--with-keycloak-test``), not the ``WITH_*_FLAG`` shell
    variable — run-dev-tests.sh's table is keyed by the flag spelling with the ``--with-``
    prefix stripped (``keycloak-test``), so this stays directly comparable without a second
    name-mapping table that could itself drift.
    """
    return {m[len("--with-") :] for m in re.findall(r"--with-[a-z0-9-]+", source)}


def _managed_flags(source: str) -> set[str]:
    """Flags that appear as a key of OVERLAY_SERVICE in run-dev-tests.sh."""
    match = re.search(r"declare -A OVERLAY_SERVICE=\((.*?)\n\)", source, re.DOTALL)
    assert match, "OVERLAY_SERVICE table not found in run-dev-tests.sh"
    return set(re.findall(r"\[([a-z0-9-]+)\]=", match.group(1)))


def _exempt_flags(source: str) -> dict[str, str]:
    """flag -> reason, from EXEMPT_WITH_FLAGS in run-dev-tests.sh."""
    match = re.search(r"declare -A EXEMPT_WITH_FLAGS=\((.*?)\n\)", source, re.DOTALL)
    assert match, "EXEMPT_WITH_FLAGS table not found in run-dev-tests.sh"
    body = match.group(1)
    result: dict[str, str] = {}
    for line in body.splitlines():
        m = re.match(r'\s*\[([a-z0-9-]+)\]="(.*)"\s*$', line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


def test_every_opentr_with_flag_is_either_managed_or_explicitly_exempt(
    opentr_source, run_dev_tests_source
):
    all_flags = _opentr_with_flags(opentr_source)
    managed = _managed_flags(run_dev_tests_source)
    exempt = _exempt_flags(run_dev_tests_source)

    unaccounted = all_flags - managed - set(exempt)
    assert not unaccounted, (
        f"these opentr.sh --with-* flags are neither in run-dev-tests.sh's OVERLAY_SERVICE "
        f"table nor its EXEMPT_WITH_FLAGS map: {sorted(unaccounted)}. A newly-added opentr.sh "
        f"overlay must be handled (auto-detected/started/torn-down) or explicitly exempted "
        f"with a written reason — see scripts/CLAUDE.md's --with-llm-test regression for why "
        f"this matters."
    )


def test_the_scanner_finds_the_known_managed_and_exempt_flags(run_dev_tests_source):
    """Must-fire control: proves the two extractors actually read the tables, not an empty
    match that would make the test above trivially pass."""
    managed = _managed_flags(run_dev_tests_source)
    exempt = _exempt_flags(run_dev_tests_source)

    assert {"mock-llm", "keycloak-test", "ldap-test", "watch", "mock-asr"} <= managed
    assert {"authentik-test", "pki", "llm-test"} <= set(exempt)


def test_every_exemption_carries_a_real_reason(run_dev_tests_source):
    exempt = _exempt_flags(run_dev_tests_source)
    assert exempt, "EXEMPT_WITH_FLAGS parsed empty — the regex is not matching the table"
    for flag, reason in exempt.items():
        assert len(reason) > 30, (
            f"--with-{flag}'s exemption reason is too short to be real: {reason!r}"
        )


def test_the_extractor_would_notice_an_unaccounted_flag():
    """Must-fire control for the assertion in the real test: a flag present in opentr.sh's
    usage but absent from both run-dev-tests.sh tables must be reported, not silently passed.
    """
    fake_opentr = "echo '  --with-newthing   - does a new thing'\n"
    fake_run_dev_tests = (
        "declare -A OVERLAY_SERVICE=(\n"
        "    [mock-llm]=mock-llm\n"
        ")\n"
        "declare -A EXEMPT_WITH_FLAGS=(\n"
        '    [pki]="needs the prod+nginx overlay, not the dev stack this script targets, long enough"\n'
        ")\n"
    )
    all_flags = _opentr_with_flags(fake_opentr)
    managed = _managed_flags(fake_run_dev_tests)
    exempt = _exempt_flags(fake_run_dev_tests)
    assert all_flags - managed - set(exempt) == {"newthing"}


def test_the_managed_flags_extractor_reads_the_real_table_shape():
    """Must-fire control for _managed_flags against the exact multi-line indentation
    run-dev-tests.sh actually uses (a single-line regex could pass here while failing on
    the real file's newlines)."""
    fake = (
        "declare -A OVERLAY_SERVICE=(\n"
        "    [mock-llm]=mock-llm\n"
        "    [keycloak-test]=keycloak\n"
        '    [watch]=""\n'
        ")\n"
    )
    assert _managed_flags(fake) == {"mock-llm", "keycloak-test", "watch"}
