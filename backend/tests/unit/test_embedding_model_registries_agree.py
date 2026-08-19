"""Every embedding model we OFFER must exist in all four registries (#504).

`paraphrase-multilingual-mpnet-base-v2` was listed in the admin UI, in the installer's
menu, in the offline downloader, in the docs and in three Python registries — and it is
**not an OpenSearch-provided pretrained model at all**. Measured on
``opensearchproject/opensearch:3.4.0`` at versions 1.0.0, 1.0.1 and 1.0.2, every
registration ended:

    REGISTER -> FAILED: "This model is not in the pre-trained model list,
                         please check your parameters."

An admin who picked it got a broken install, and an operator who chose it in
`setup-opentranscribe.sh` got a download that could never succeed. It had been offered
in **nine** files.

Nothing could catch that, because the four registries are four separate literals that
nothing compared:

  1. ``core/constants.OPENSEARCH_EMBEDDING_MODELS``   - what the admin UI offers
  2. ``search/ml_model_service._MODEL_FILE_PATTERNS`` - offline file:// registration
  3. ``search/model_downloader._OPENSEARCH_MODEL_REGISTRY`` - the in-app downloader
  4. ``scripts/download-models.py``                   - the offline packaging download

A model in (1) and not (2) cannot be registered offline. In (1) and not (3)/(4) cannot
be pre-fetched. In (2)-(4) but not (1) is dead weight nobody can select.

⚠️ This checks CONSISTENCY, not existence. Only a real cluster can say whether
OpenSearch actually provides a model — that is
``scripts/verify-embedding-models.py``, which registers, deploys and runs a real
prediction. Keep both: this one is free and runs on every commit; that one needs a
cluster and catches what no static check can.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOWNLOAD_SCRIPT = _REPO_ROOT / "scripts" / "download-models.py"

#: Measured on opensearch 3.4.0, 2026-08-18. A model here is one that registered,
#: deployed AND returned its declared dimension from a real prediction.
VERIFIED_WORKING = {
    "all-MiniLM-L6-v2",
    "all-mpnet-base-v2",
    "all-distilroberta-v1",
    "paraphrase-multilingual-MiniLM-L12-v2",
    "distiluse-base-multilingual-cased-v1",
    "multi-qa-MiniLM-L6-cos-v1",
}

#: Measured and rejected. Re-adding any of these needs a fresh measurement, not an
#: assumption — each failed for its own reason (see constants.py for the detail).
VERIFIED_BROKEN = {
    "paraphrase-multilingual-mpnet-base-v2",  # not an OpenSearch-provided model
    "msmarco-distilbert-base-tas-b",  # dot-product; control 0.703 under cosine
    "multi-qa-mpnet-base-dot-v1",  # dot-product; control 0.385
    "paraphrase-MiniLM-L3-v2",  # DEPLOY FAILED
}


def _short(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _offered() -> set[str]:
    from app.core.constants import OPENSEARCH_EMBEDDING_MODELS

    return {_short(name) for name in OPENSEARCH_EMBEDDING_MODELS}


def _file_patterns() -> set[str]:
    from app.services.search.ml_model_service import _MODEL_FILE_PATTERNS

    return {_short(name) for name in _MODEL_FILE_PATTERNS}


def _downloader_registry() -> set[str]:
    from app.services.search.model_downloader import _OPENSEARCH_MODEL_REGISTRY

    return {_short(name) for name in _OPENSEARCH_MODEL_REGISTRY}


def _download_script_models() -> set[str]:
    """Model short names named in scripts/download-models.py.

    Parsed rather than imported: the script imports torch at module scope, which the
    CPU-only CI image does not carry, and importing it would run its patching side
    effects. The names appear as `huggingface/sentence-transformers/<short>` literals.
    """
    source = _DOWNLOAD_SCRIPT.read_text(encoding="utf-8")
    return set(re.findall(r"huggingface/sentence-transformers/([\w.-]+)", source))


def test_every_offered_model_is_one_that_was_measured_working() -> None:
    """The defect: the UI offered a model that cannot be registered at all."""
    unverified = sorted(_offered() - VERIFIED_WORKING)
    assert not unverified, (
        "These models are offered to admins but are not in the verified-working set. "
        "Run `python3 scripts/verify-embedding-models.py --url <throwaway>` against a "
        f"real cluster and add them here only if they pass: {unverified}"
    )


def test_no_registry_still_carries_a_model_measured_as_broken() -> None:
    """A model that fails to register must not survive anywhere.

    The phantom lived in nine files; removing it from the UI alone would have left the
    downloader still trying to fetch it.
    """
    everywhere = {
        "constants.OPENSEARCH_EMBEDDING_MODELS": _offered(),
        "ml_model_service._MODEL_FILE_PATTERNS": _file_patterns(),
        "model_downloader._OPENSEARCH_MODEL_REGISTRY": _downloader_registry(),
        "scripts/download-models.py": _download_script_models(),
    }
    offenders = {
        where: sorted(names & VERIFIED_BROKEN)
        for where, names in everywhere.items()
        if names & VERIFIED_BROKEN
    }
    assert not offenders, f"a model measured as broken is still registered: {offenders}"


@pytest.mark.parametrize(
    "registry_name",
    [
        "ml_model_service._MODEL_FILE_PATTERNS",
        "model_downloader._OPENSEARCH_MODEL_REGISTRY",
        "scripts/download-models.py",
    ],
)
def test_every_offered_model_can_also_be_fetched_and_registered_offline(
    registry_name: str,
) -> None:
    """Offering a model the offline path cannot handle is an airgap failure.

    Each of these three registries is what makes an offered model actually usable
    without internet — the file pattern for `file://` registration, and the two
    downloaders that put the artifact on disk in the first place.
    """
    registries = {
        "ml_model_service._MODEL_FILE_PATTERNS": _file_patterns(),
        "model_downloader._OPENSEARCH_MODEL_REGISTRY": _downloader_registry(),
        "scripts/download-models.py": _download_script_models(),
    }
    missing = sorted(_offered() - registries[registry_name])
    assert not missing, (
        f"{registry_name} does not know about {missing}, so those models are offered in "
        "the admin UI but cannot be pre-fetched or registered offline"
    )


def test_the_verified_sets_do_not_overlap() -> None:
    """Guard the guard: a model cannot be both verified working and verified broken."""
    assert not (VERIFIED_WORKING & VERIFIED_BROKEN)


def test_the_download_script_is_parseable_and_names_models() -> None:
    """If the parse silently returned nothing, the checks above would pass vacuously.

    This is the failure shape the repo's own auditors exist to catch: a scanner that
    matches nothing reports zero findings and reads exactly like a clean tree.
    """
    ast.parse(_DOWNLOAD_SCRIPT.read_text(encoding="utf-8"))
    found = _download_script_models()
    assert len(found) >= 4, f"parsed only {found} from download-models.py — check the pattern"
