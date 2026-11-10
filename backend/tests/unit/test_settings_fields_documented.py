"""Every ``Settings`` field must be findable by an operator.

``test_env_example_coverage`` checks the template -> code direction (a documented key
must be read by something). This is the other direction: a field the code declares must
appear in ``.env.example`` or in the environment-variables reference, unless it is on
``INTERNAL`` below with a written reason. Without it a new tunable ships as a setting
nobody can discover.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_DOCS = REPO_ROOT / "docs-site" / "docs" / "configuration" / "environment-variables.md"

# Declared fields an operator is not expected to set. Keep each reason specific.
INTERNAL: dict[str, str] = {
    "API_PREFIX": "URL prefix the frontend and nginx are built around; changing it breaks the SPA.",
    "DB_APPLICATION_NAME": "compose sets it per service (opentranscribe-api / worker) for pg_stat_activity.",
    "ENCRYPTION_ALGORITHM_V3": "wire-format constant for stored ciphertext; changing it orphans existing secrets.",
    "MODEL_BASE_DIR": "container path derived from MODELS_DIR, which compose sets from MODEL_CACHE_DIR.",
    "OPENSEARCH_CHUNKS_INDEX": "index name fixed by the schema bootstrap; renaming it orphans the data.",
    "OPENSEARCH_SEARCH_PIPELINE": "pipeline name fixed by the schema bootstrap.",
    "OPENSEARCH_SPEAKER_INDEX": "index name fixed by the schema bootstrap; renaming it orphans the data.",
    "OPENSEARCH_SUMMARY_INDEX": "index name fixed by the schema bootstrap; renaming it orphans the data.",
    "OPENSEARCH_TRANSCRIPT_INDEX": "index name fixed by the schema bootstrap; renaming it orphans the data.",
    "PROJECT_NAME": "FastAPI title / email footer label; cosmetic.",
    "WATCH_FOLDER_PATH": "in-container path docker-compose.watch.yml always sets to /watch; host side is WATCH_HOST_PATH.",
}


def _mentioned(text: str, key: str) -> bool:
    return re.search(rf"(?<![A-Z0-9_]){re.escape(key)}(?![A-Z0-9_])", text) is not None


def undocumented_fields(
    fields: set[str], example: str, docs: str, internal: dict[str, str]
) -> set[str]:
    """Fields found in neither the template nor the reference and not allowlisted."""
    return {
        f
        for f in fields
        if f not in internal and not _mentioned(example, f) and not _mentioned(docs, f)
    }


def test_every_settings_field_is_documented_or_declared_internal():
    missing = undocumented_fields(
        set(Settings.model_fields),
        ENV_EXAMPLE.read_text(encoding="utf-8"),
        ENV_DOCS.read_text(encoding="utf-8"),
        INTERNAL,
    )
    assert not missing, (
        f"Settings fields with no .env.example line and no entry in {ENV_DOCS.name}: "
        f"{sorted(missing)}. Document each, or add it to INTERNAL with a reason."
    )


def test_internal_allowlist_has_no_stale_entries():
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    docs = ENV_DOCS.read_text(encoding="utf-8")
    fields = set(Settings.model_fields)
    stale = {
        k for k in INTERNAL if k not in fields or _mentioned(example, k) or _mentioned(docs, k)
    }
    assert not stale, f"INTERNAL entries that are gone or now documented: {sorted(stale)}"


def test_the_detector_fires_on_an_undocumented_field():
    assert undocumented_fields({"BRAND_NEW_KNOB"}, "A=1\n", "B=2\n", {}) == {"BRAND_NEW_KNOB"}


def test_the_detector_stays_quiet_on_documented_and_allowlisted_fields():
    assert (
        undocumented_fields(
            {"IN_EXAMPLE", "IN_DOCS", "ALLOWED"}, "#IN_EXAMPLE=1\n", "IN_DOCS=2", {"ALLOWED": "r"}
        )
        == set()
    )


def test_the_detector_does_not_match_a_longer_name():
    assert undocumented_fields({"PORT"}, "BACKEND_PORT=1\n", "", {}) == {"PORT"}
