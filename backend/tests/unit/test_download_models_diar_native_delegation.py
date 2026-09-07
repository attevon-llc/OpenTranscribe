"""download-models.py must DELEGATE diar-native provisioning, never re-implement it.

`scripts/download-models.py --only diar-native` and `main.py`'s FastAPI lifespan both need
to provision the native diarizer's ONNX/PLDA model set. They used to be TWO implementations:
`app/transcription/native_provision.py` (the canonical one -- DB/env-resolved model set and
timeout, the blank-`HF_ENDPOINT` scrub, the `DEPLOYMENT_MODE=lite` skip) and a second,
hand-rolled copy inside `download-models.py` with its own hardcoded `'fast'` model set, its
own `1800`s timeout, its own exit-code table and remedy strings, and neither the scrub nor
the lite skip. Divergence between the two is the defect this guards against, not any single
bug in the fork.

`app.transcription.native_provision` is only importable from inside the backend container
(see `download-models.py`'s own module docstring for why), so this asserts on the SOURCE of
the delegating call and the absence of a re-forked copy, rather than executing the
provisioning path itself -- that real, end-to-end path is exercised in
`backend/tests/unit/test_native_provision.py` and was verified manually against the running
backend container when this test was added.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"
NATIVE_PROVISION_PY = REPO_ROOT / "backend" / "app" / "transcription" / "native_provision.py"

pytestmark = pytest.mark.skipif(
    not DOWNLOADER_PY.exists() or not NATIVE_PROVISION_PY.exists(),
    reason="download-models.py or native_provision.py not present in this checkout",
)


def _function_source(script: Path, name: str) -> str:
    """Return the source of a top-level function, by AST rather than a line-range regex."""
    source = script.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"could not recover source for {name}() in {script.name}"
            return segment
    raise AssertionError(f"{name}() not found in {script.name}")


def _module_level_assigned_names(script: Path) -> set[str]:
    """Names bound by a simple top-level ``NAME = ...`` assignment."""
    tree = ast.parse(script.read_text())
    return {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }


def test_diar_native_delegates_to_the_canonical_implementation():
    """download_diar_native_models() must call ensure_native_models, not shell out itself."""
    source = _function_source(DOWNLOADER_PY, "download_diar_native_models")

    assert "ensure_native_models" in source, (
        "download_diar_native_models() no longer calls "
        "app.transcription.native_provision.ensure_native_models -- that is the ONLY "
        "sanctioned way to provision the diar-native model set (see that module's own "
        "docstring for why the export logic lives in diar-server, not in a Python caller). "
        "Re-adding a direct diar-server subprocess call here re-forks provisioning into a "
        "second implementation."
    )
    assert "from app.transcription.native_provision import ensure_native_models" in source, (
        "download_diar_native_models() must import ensure_native_models from "
        "app.transcription.native_provision specifically, not from a local re-export or a "
        "renamed copy"
    )


def test_diar_native_does_not_reimplement_diar_server_invocation():
    """Nothing in download-models.py may build a `diar-server provision-models` argv.

    That argv (``--models-dir``/``--set``/``--mode``/``--smoke-clip``/``--json``, the exit
    codes, the remedies, the timeout) is ``ensure_native_models``'s job alone. Building it a
    second time is exactly how the model set, timeout, and remedy table drifted before: a
    hardcoded ``'fast'`` instead of resolving ``DIAR_NATIVE_MODEL_SET``, a private ``1800``s
    timeout instead of ``DIAR_NATIVE_PROVISION_TIMEOUT_S``, and a remedy table with no
    ``--lite`` awareness.
    """
    source = DOWNLOADER_PY.read_text()

    # Each snippet is something ONLY a direct diar-server invocation would need; none of
    # them have any other legitimate reason to appear in this file post-delegation.
    banned_snippets = (
        "provision-models",  # the diar-server subcommand -- only native_provision invokes it
        "subprocess.run",  # this script no longer shells out for diar-native at all
        "shutil.which",  # binary discovery belongs to native_provision, not this script
    )
    found = [snippet for snippet in banned_snippets if snippet in source]
    assert not found, (
        f"download-models.py contains {found} -- these belong only in "
        "app/transcription/native_provision.py. Their presence here means diar-native "
        "provisioning has been re-forked into a second implementation."
    )


def test_diar_native_does_not_redefine_native_provisions_constants():
    """No second copy of native_provision's exit codes / remedy table / timeout constant.

    Keyed off the REAL symbol list native_provision.py defines (``EXIT_*``), so this cannot
    go stale if that module renames or adds a code -- it never hand-copies the names.
    """
    canonical_exit_codes = {
        name
        for name in _module_level_assigned_names(NATIVE_PROVISION_PY)
        if name.startswith("EXIT_")
    }
    assert canonical_exit_codes, (
        "sanity check failed: no EXIT_* constants found in native_provision.py"
    )

    downloader_names = _module_level_assigned_names(DOWNLOADER_PY)

    # A prior fork mirrored each canonical name with a "_DIAR_" prefix (EXIT_TOKEN_DENIED ->
    # _DIAR_EXIT_TOKEN_DENIED) plus two more of its own (_DIAR_REMEDY, _DIAR_PROVISION_TIMEOUT_S).
    # Reject the exact canonical names AND that mirrored shape, so a re-fork under a
    # slightly different name still fails loudly instead of slipping past an exact-match check.
    exact_collisions = canonical_exit_codes & downloader_names
    mirrored_collisions = {
        name
        for name in downloader_names
        if name.startswith("_DIAR_EXIT_") or name in ("_DIAR_REMEDY", "_DIAR_PROVISION_TIMEOUT_S")
    }
    offenders = exact_collisions | mirrored_collisions
    assert not offenders, (
        f"download-models.py redefines provisioning constant(s) {sorted(offenders)} -- these "
        "belong only in app/transcription/native_provision.py. A second copy is exactly the "
        "divergent-implementation bug this test exists to catch."
    )


def test_diar_native_lite_comment_is_not_false():
    """Lite has neither a missing binary NOR a `DEPLOYMENT_MODE=lite` skip any more.

    Two claims used to be made about lite here, and both are now false:

    1. "the lite image has no diar-server binary" -- `backend/Dockerfile.lite` copies the
       binary in explicitly (issue #660) so lite can serve diarization on the CPU provider.
    2. "provisioning skips on `DEPLOYMENT_MODE=lite`" -- issue #654 removed that skip from
       `native_provision.ensure_native_models` entirely: `requirements-lite.txt` now installs
       the export toolchain (`pyannote.audio`, `onnx`, `onnxscript`, `onnxslim`,
       `onnxconverter-common`) too, so a lite install provisions its own weights exactly like
       the full image, on first boot. There is nothing left for `download-models.py` to skip
       "identically" to, since nothing skips.

    A bare substring check for the old binary-absence wording would pass even if this file
    picked up a NEW false claim shaped like #2 (e.g. reintroducing "the lite skip" prose) or a
    reworded version of #1, so this also windows around every "lite"+"skip" co-occurrence and
    fails unless it reads as a NEGATION of the skip ("no ... skip", "removed", "no longer") --
    the correct, present-tense framing (issue #654) -- rather than an assertion that lite
    skips something.
    """
    source = DOWNLOADER_PY.read_text()
    lowered = source.lower()
    assert "no diar-server binary" not in lowered, (
        "download-models.py still claims the lite image lacks the diar-server binary -- "
        "backend/Dockerfile.lite ships it (issue #660)."
    )

    # Deliberately narrow to phrases where a negator directly modifies "skip" itself --
    # a bare "not "/"removed" anywhere in the surrounding window is too generic (an early
    # draft matched on an unrelated "this is a caller of it, not a fork of it" two lines
    # away and reported a false negative).
    negation_re = re.compile(r"(?:\bno\b|\bnot\b|\bremoved\b|\bno longer\b)[^.]{0,40}\bskip")
    lines = lowered.splitlines()
    window = 3
    for i, line in enumerate(lines):
        if "lite" not in line or "skip" not in line:
            continue
        lo, hi = max(0, i - window), min(len(lines), i + window + 1)
        context = "\n".join(lines[lo:hi])
        assert negation_re.search(context), (
            f"download-models.py line {i + 1} mentions 'lite' and 'skip' without a negation "
            "nearby -- issue #654 removed the DEPLOYMENT_MODE=lite skip from "
            "native_provision.ensure_native_models entirely, so any surviving mention must "
            f"read as 'lite no longer skips', not 'lite skips'. Line: {line!r}"
        )
