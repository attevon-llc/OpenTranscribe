"""``requirements-lite.txt`` must directly declare every export-critical distribution.

This is the check the ``test_native_provision.py`` docstring claimed already existed:
"``requirements-lite.txt`` now installs the four packages the shipped binary's own
preflight names — verified by grepping the binary inside the built lite image:
``pyannote.audio``, ``onnxscript``, ``onnxslim``, ``onnxconverter_common``." No such grep
exists anywhere in this repo, and the real list — ``EXPORTER_IMPORTS`` in
``tests/integration/test_export_toolchain_in_shipped_images.py``, DERIVED from grepping
the actual shipped binary's embedded import statements — has **eleven** entries, not four.
That stale "four" is what let ``onnxruntime`` go missing from ``requirements-lite.txt``
once and ship a lite image that could not export the gated ONNX/PLDA model set at all
(the incident ``test_export_toolchain_in_shipped_images.py``'s module docstring
describes).

Deliberately a UNIT test, not integration: it reads the requirements file on disk, so it
needs no live stack, no built image, and no download — it runs in the fast suite and in
CI, where the two-line "onnxruntime went missing" defect would otherwise sail through
unnoticed for as long as nobody happened to look at a diff by eye.

Only DIRECT declarations are asserted. ``transformers`` and ``huggingface_hub`` are
excluded on purpose: requirements-lite.txt's own comment (near its ``onnxruntime`` pin)
records they arrive transitively via ``pyannote.audio`` and ``sentence-transformers``,
and asserting them as direct pins would fail the moment either upstream dependency stops
needing them for a reason that has nothing to do with lite's export capability.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.integration.test_export_toolchain_in_shipped_images import EXPORTER_IMPORTS

#: Arrive transitively (see this module's docstring) — must not be asserted as direct.
_LITE_TRANSITIVE_IMPORTS = frozenset({"transformers", "huggingface_hub"})

#: Import name -> the distribution name it is pinned under in requirements-lite.txt.
#: Only differs from the import name where PyPI's normalised name uses a hyphen the
#: Python import statement cannot (``onnxconverter_common`` -> ``onnxconverter-common``).
_IMPORT_TO_DISTRIBUTION = {"onnxconverter_common": "onnxconverter-common"}

_LITE_REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements-lite.txt"


def test_lite_requirements_declare_every_direct_export_dependency() -> None:
    assert _LITE_REQUIREMENTS.is_file(), f"not found: {_LITE_REQUIREMENTS}"
    text = _LITE_REQUIREMENTS.read_text(encoding="utf-8")

    direct_required = sorted(set(EXPORTER_IMPORTS) - _LITE_TRANSITIVE_IMPORTS)
    assert direct_required, "EXPORTER_IMPORTS resolved to nothing — the import is broken"

    missing = []
    for import_name in direct_required:
        distribution = _IMPORT_TO_DISTRIBUTION.get(import_name, import_name)
        # Pinned lines look like `torch==2.11.0+cpu` or `pyannote.audio==4.0.7` — anchor
        # on start-of-line so `onnx==` does not accidentally match-search `onnxruntime==`.
        pattern = re.compile(rf"^{re.escape(distribution)}(\[[^\]]*\])?==", re.MULTILINE)
        if not pattern.search(text):
            missing.append(distribution)

    assert not missing, (
        f"requirements-lite.txt is missing a direct pin for: {missing}. These are "
        f"EXPORTER_IMPORTS entries that diar-server's embedded export scripts import "
        f"unguarded and that do not arrive transitively in this file — a lite image "
        f"missing any of them cannot export the gated ONNX/PLDA model set, which is "
        f"lite's only way to obtain a local voiceprint path at all."
    )


def test_lite_transitive_exclusion_list_is_not_hiding_a_real_gap() -> None:
    """Guard the guard: every excluded name must genuinely be undeclared directly.

    If ``transformers``/``huggingface_hub`` ever gained a direct pin (e.g. a future
    dependency needs a specific version), that is fine — but if one of them were REMOVED
    from ``EXPORTER_IMPORTS`` while still being required, this exclusion list would go
    stale silently. This does not fail the build; it documents the assumption is still
    live by asserting the excluded names are still present in EXPORTER_IMPORTS at all.
    """
    # Asserted OUTSIDE the loop: an empty _LITE_TRANSITIVE_IMPORTS would make the loop
    # below run zero times and pass vacuously, silently disabling this guard entirely.
    assert _LITE_TRANSITIVE_IMPORTS, "_LITE_TRANSITIVE_IMPORTS is empty — nothing to guard"
    for import_name in _LITE_TRANSITIVE_IMPORTS:
        assert import_name in EXPORTER_IMPORTS, (
            f"{import_name!r} is excluded here as 'transitive' but is no longer in "
            f"EXPORTER_IMPORTS — this exclusion is now meaningless and should be removed"
        )
