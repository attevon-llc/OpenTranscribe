"""`release-criteria.yaml` and the stage scripts must agree — checked statically.

The file's own header states the rule for adding a stage: "wire it the same way —
bidirectionally, or not at all". Bidirectional enforcement already exists at RUNTIME
(`criteria-lib.sh` refuses an undeclared id, and `criteria_assert_all_checked` refuses a
declared-but-unrecorded one). That is the authoritative check, but it only fires when the
stage actually runs — and most stages cannot run on a developer machine: they need built
multi-gigabyte images, a pushed tag, or Docker Hub credentials.

So the same contract is asserted here, statically, in the fast unit suite. Without it, a
criterion declared for `publish` and never recorded there would be discovered only by a real
release — which is precisely the moment it is most expensive to discover.

This is deliberately NOT a re-implementation of the runtime check: it reads the same YAML and
greps the same scripts for `record <id>`. A stage that passes here can still fail at runtime
(the outcome word must be valid, and every declared id must be recorded on *every* code path);
a stage that fails here is definitely broken.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CRITERIA_YAML = REPO_ROOT / "scripts" / "release" / "release-criteria.yaml"
RELEASE_DIR = REPO_ROOT / "scripts" / "release"


# stage id -> the script that owns it. Derived from the NN-<stage>.sh naming convention
# rather than hardcoded, so a renamed stage script fails loudly instead of silently
# dropping out of this check.
def _script_for(stage: str) -> Path | None:
    matches = sorted(RELEASE_DIR.glob(f"[0-9][0-9]-{stage}.sh"))
    return matches[0] if matches else None


def _require_script(stage: str) -> Path:
    """`_script_for` for the callers that cannot proceed without one.

    Deliberately raises rather than being typed away with a cast: a stage that declares
    criteria and has no script is a real defect (the criteria would be enforced by nothing),
    and it must surface as a named test failure, not an AttributeError on None three lines
    later. This is what closes mypy's `union-attr` on every call site below — by removing the
    None from the type, not by silencing the check.
    """
    script = _script_for(stage)
    assert script is not None, (
        f"stage '{stage}' declares criteria but there is no scripts/release/NN-{stage}.sh to "
        f"enforce them"
    )
    return script


def _doc() -> dict[str, Any]:
    # Annotated because yaml.safe_load is untyped (-> Any); every downstream subscript is
    # still checked against dict[str, Any].
    doc: dict[str, Any] = yaml.safe_load(CRITERIA_YAML.read_text())
    return doc


def _stages_with_criteria() -> list[str]:
    return sorted(s for s, body in _doc()["stages"].items() if (body or {}).get("criteria"))


def test_every_declared_stage_is_in_the_order_list_and_vice_versa_is_documented():
    doc = _doc()
    order = doc["order"]
    declared = set(doc["stages"])
    stray = sorted(declared - set(order))
    assert not stray, (
        f"{stray} declare criteria but are not stages in `order:` — either they are not real "
        f"stages, or `order:` is missing them"
    )


def test_the_stage_script_naming_convention_still_resolves():
    """Guard the guard: if the NN-<stage>.sh convention changed, every check below goes vacuous.

    Every stage in `order:` must resolve, not a hand-picked sample — a sample is what lets a
    newly added stage drop out of the parametrised checks below without anything going red.
    """
    order = _doc()["order"]
    assert len(order) >= 12, f"order: has shrunk to {order} — expected the full 12-stage pipeline"

    resolved = {stage: _script_for(stage) for stage in order}
    unresolved = sorted(stage for stage, path in resolved.items() if path is None)
    assert not unresolved, (
        f"no scripts/release/NN-<stage>.sh for {unresolved} — the naming convention this "
        f"module derives from has changed, and these checks would silently stop covering "
        f"anything for those stages"
    )
    # Concrete, not bare-truthy: the resolved file must be the one whose name ENDS in the
    # stage. A `_script_for` that returned the first script in the directory for everything
    # would satisfy `is not None` for all 12 and break every check below.
    found = {stage: path for stage, path in resolved.items() if path is not None}
    misresolved = sorted(
        f"{stage} -> {path.name}"
        for stage, path in found.items()
        if path.name != f"{path.name[:3]}{stage}.sh"
    )
    assert not misresolved, f"_script_for resolved the wrong file: {misresolved}"


@pytest.mark.parametrize("stage", _stages_with_criteria())
def test_every_declared_criterion_is_recorded_by_its_stage_script(stage):
    script = _require_script(stage)
    text = script.read_text()
    # Strip comments: several scripts explain a criterion in prose, and a mention is not a
    # recording. (The same trap caught in the pre-commit files: patterns matched the comment
    # that documented the removed code.)
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())

    declared = [c["id"] for c in _doc()["stages"][stage]["criteria"]]
    missing = [cid for cid in declared if not re.search(rf"\brecord\s+{re.escape(cid)}\b", code)]
    assert not missing, (
        f"{script.name} never records {missing}, which release-criteria.yaml declares for "
        f"stage '{stage}'. At runtime criteria_assert_all_checked exits 2 on this; most stages "
        f"cannot be run locally, so it would otherwise surface during a real release."
    )


@pytest.mark.parametrize("stage", _stages_with_criteria())
def test_every_recorded_id_is_declared(stage):
    """The other direction: a `record` of an id the file does not define is exit 2 at runtime."""
    script = _require_script(stage)
    code = "\n".join(line.split("#", 1)[0] for line in script.read_text().splitlines())
    declared = {c["id"] for c in _doc()["stages"][stage]["criteria"]}

    recorded = set(re.findall(r"\brecord\s+([a-z0-9][a-z0-9-]*)\b", code))
    # `record` is also the name of the helper's own definition line in criteria-lib.sh; the
    # stage scripts only ever call it.
    undeclared = sorted(recorded - declared)
    assert not undeclared, (
        f"{script.name} records {undeclared}, which release-criteria.yaml does not declare "
        f"for stage '{stage}' — that is exit 2 at runtime."
    )


@pytest.mark.parametrize("stage", _stages_with_criteria())
def test_every_criteria_consuming_stage_sources_the_shared_library(stage):
    """Four hand-rolled copies of the contract is four chances for them to disagree.

    preflight and verify predate criteria-lib.sh and still carry their own inline copies;
    they are exempt with that written reason. Anything NEW must use the library.
    """
    predates_library = {"preflight", "verify"}
    if stage in predates_library:
        pytest.skip(f"{stage} predates criteria-lib.sh and carries its own inline copy")
    script = _require_script(stage)
    assert "criteria-lib.sh" in script.read_text(), (
        f"{script.name} declares criteria but does not source criteria-lib.sh — "
        f"it would need its own copy of the bidirectional contract, which is what the library "
        f"exists to prevent"
    )


def _unrecorded(declared: list[str], code: str) -> list[str]:
    """The matching rule both directions above use, isolated so it can be given a control."""
    return [cid for cid in declared if not re.search(rf"\brecord\s+{re.escape(cid)}\b", code)]


def test_the_recording_check_actually_fires_on_an_unrecorded_criterion():
    """Must-fire control.

    Without this, `test_every_declared_criterion_is_recorded_by_its_stage_script` passes
    identically whether the regex works or matches nothing — the "detector that matches
    nothing" shape this repo has shipped before. Most stages cannot be executed locally, so
    this static check is the only thing standing between a mis-wired criterion and a real
    release; it has to be known-falsifiable.
    """
    code = "record alpha pass\nrecord beta fail 'detail'\n"
    assert _unrecorded(["alpha", "beta"], code) == [], "control: both are recorded"
    assert _unrecorded(["alpha", "gamma"], code) == ["gamma"], (
        "the check did not notice a declared criterion that is never recorded"
    )

    # A criterion named only in a COMMENT must not count as recorded: several stage scripts
    # explain a criterion in prose right beside the code, and a mention is not a recording.
    commented = "# record gamma pass  -- explaining why gamma exists\nrecord alpha pass\n"
    stripped = "\n".join(line.split("#", 1)[0] for line in commented.splitlines())
    assert _unrecorded(["gamma"], stripped) == ["gamma"], (
        "a criterion mentioned only in a comment was counted as recorded"
    )

    # And a prefix must not satisfy a longer id (`scan` must not cover `scan-arm64`).
    assert _unrecorded(["alpha-extra"], code) == ["alpha-extra"], (
        "a shorter recorded id satisfied a longer declared one"
    )


@pytest.mark.parametrize("stage", _stages_with_criteria())
def test_every_declared_criterion_has_a_description_and_severity(stage):
    criteria = _doc()["stages"][stage]["criteria"]
    # Asserted OUTSIDE the loop first: a stage whose `criteria:` list somehow came back empty
    # would run this loop zero times and pass, which is the shape scripts/audit-tests.py's
    # loop-only detector exists to catch. criteria-lib.sh exits 3 on an empty list at runtime,
    # so an empty one here is a real defect, not a vacuous case.
    assert criteria, f"stage '{stage}' has an empty criteria list"

    for criterion in criteria:
        assert criterion.get("description"), f"{stage}:{criterion['id']} has no description"
        assert criterion.get("severity") in {"blocking", "warn"}, (
            f"{stage}:{criterion['id']} severity is "
            f"{criterion.get('severity')!r}; must be 'blocking' or 'warn'"
        )
