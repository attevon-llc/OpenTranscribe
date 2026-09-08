"""Cleanup must key on the test COMPOSE PROJECT as well as the release-test label.

``gr_cleanup`` removed containers matching ``label=$TEST_LABEL``. That label is stamped onto
the compose files present in the staged tree at staging time — but the installer then
downloads *further* overlays into that same directory afterwards
(``docker-compose.mock-asr.yml``, ``docker-compose.mock-llm.yml``), so their services are
created carrying no release-test label at all.

Measured 2026-09-07: lite-mode's ``opentranscribe-mock-asr`` and ``opentranscribe-mock-llm``
survived ``--cleanup`` and kept ports 5198/5199 bound. The next rehearsal then refused at
phase 00 —

    [guardrails] ✗ FATAL: required ports already in use: 5198 5199

— failing **all three** scenarios for a reason that had nothing to do with the release.
``ot-reltest-lite-diar-native-1`` leaked the same way.

⚠️ **This is an ORDERING gap, and labelling more files cannot close it.** Anything created
after the labelling step is unlabelled by construction. ``TEST_PROJECT_NAME`` is the
ordering-independent key: ``gr_check_project_name`` already refuses to run unless it starts
with ``ot-reltest-``, so no real deployment can occupy that namespace — the same "filter by
compose project, never by a bare name prefix" rule the rest of the harness follows (issue
#693, where a name-prefix filter destroyed 17 live containers).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARDRAILS = REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"

pytestmark = pytest.mark.skipif(
    not GUARDRAILS.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/lib/guardrails.sh or bash is not present in this checkout",
)


def _gr_cleanup_body() -> str:
    text = GUARDRAILS.read_text(encoding="utf-8")
    match = re.search(r"^gr_cleanup\(\) \{(?P<body>.*?)^\}", text, re.S | re.M)
    assert match, "gr_cleanup has moved or been renamed; re-point this guard"
    return match.group("body")


def test_cleanup_selects_containers_by_the_test_compose_project():
    body = _gr_cleanup_body()
    assert "com.docker.compose.project=$TEST_PROJECT_NAME" in body, (
        "gr_cleanup selects containers only by the release-test label. Any service created "
        "from a compose file the installer added AFTER the labelling step carries no such "
        "label and survives cleanup — that is how mock-asr/mock-llm kept ports 5198/5199 "
        "bound and made the next rehearsal refuse at phase 00, on all three scenarios."
    )


def test_cleanup_still_selects_by_the_release_test_label():
    """The project key ADDS to the label key; it must not replace it.

    Scenario A's stock-named stack runs under the `opentranscribe` project, not
    `ot-reltest-*`, so dropping the label filter would stop cleaning it up.
    """
    body = _gr_cleanup_body()
    assert "label=$TEST_LABEL" in body, (
        "gr_cleanup no longer filters by TEST_LABEL. The stock-project containers a "
        "scenario creates are labelled but do NOT run under TEST_PROJECT_NAME, so they "
        "would stop being cleaned up."
    )


def test_the_project_name_prefix_is_enforced_before_it_is_used_as_a_cleanup_key():
    """The safety argument, asserted rather than assumed.

    Removing everything in a compose project is only safe because that project name is
    guaranteed to be a test namespace. If the `ot-reltest-` check ever weakens, this
    cleanup key becomes a way to delete an arbitrary project.
    """
    text = GUARDRAILS.read_text(encoding="utf-8")
    match = re.search(r"^gr_check_project_name\(\) \{(?P<body>.*?)^\}", text, re.S | re.M)
    assert match, "gr_check_project_name has moved; the safety argument needs re-checking"
    assert "ot-reltest-*" in match.group("body"), (
        "gr_check_project_name no longer requires TEST_PROJECT_NAME to start with "
        "'ot-reltest-'. Cleaning up by compose project is only safe while it does."
    )
    assert "gr_die" in match.group("body"), (
        "gr_check_project_name no longer ABORTS on a bad project name — it must refuse, "
        "not warn, since a later step deletes everything in that project"
    )
