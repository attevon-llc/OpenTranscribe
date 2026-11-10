"""Characterization of ``Settings`` across the split into ``settings_*`` mixins (v0.7.0 L07).

The split moves fields verbatim into mixin base classes. Pydantic collects fields and
decorators from every base in the MRO (pydantic docs, "Models -> Inheritance" and
"Validators"), so the assembled ``Settings`` must be indistinguishable from the single-class
original. The snapshot is taken in a subprocess with a scrubbed environment so a developer's
shell cannot change a default and so the module-level ``settings = Settings()`` is evaluated
fresh.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "settings_defaults_head.json"
BACKEND_DIR = Path(__file__).resolve().parents[2]

_SNAPSHOT_CODE = """
import json, sys
from pydantic_core import PydanticUndefined
from app.core.config import Settings

fields = {}
for name, info in Settings.model_fields.items():
    default = "<required>" if info.default is PydanticUndefined else repr(info.default)
    fields[name] = {"default": default, "annotation": repr(info.annotation)}
decorators = Settings.__pydantic_decorators__
out = {
    "fields": fields,
    "field_validators": sorted(decorators.field_validators),
    "model_validators": sorted(decorators.model_validators),
    "members": sorted(
        n for n in dir(Settings)
        if not n.startswith("__") and n not in Settings.model_fields
        and not n.startswith("model_") and n not in ("construct", "copy", "dict", "json",
            "from_orm", "parse_obj", "parse_raw", "parse_file", "schema", "schema_json",
            "update_forward_refs", "validate", "settings_customise_sources")
    ),
}
json.dump(out, sys.stdout, sort_keys=True, indent=1)
"""


def snapshot(tmp_dir: Path) -> dict[str, Any]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "TESTING": "1",
        "TEMP_DIR": str(tmp_dir),
        "PYTHONPATH": str(BACKEND_DIR),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_CODE],
        env=env,
        cwd=tmp_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    # TEMP_DIR's default embeds the per-run directory; normalise it.
    parsed: dict[str, Any] = json.loads(proc.stdout.replace(str(tmp_dir), "<TEMP_DIR>"))
    return parsed


@pytest.fixture(scope="module")
def current(tmp_path_factory):
    return snapshot(tmp_path_factory.mktemp("settings_snap"))


@pytest.fixture(scope="module")
def head():
    return json.loads(FIXTURE.read_text())


def test_field_names_equal_head(current, head):
    assert sorted(current["fields"]) == sorted(head["fields"])


def test_every_default_and_annotation_equals_head(current, head):
    drift = {
        n: (head["fields"][n], current["fields"][n])
        for n in head["fields"]
        if current["fields"].get(n) != head["fields"][n]
    }
    assert drift == {}


def test_validators_and_members_equal_head(current, head):
    assert current["field_validators"] == head["field_validators"]
    assert current["model_validators"] == head["model_validators"]
    assert current["members"] == head["members"]


def test_head_snapshot_is_not_vacuous(head):
    assert len(head["fields"]) > 300
    assert "_clamp_chunks_index_topology" in head["field_validators"]
    assert "validate_auth_settings" in head["model_validators"]


# -- one validator per split mixin still fires on the assembled Settings ---------------------


def _make(**kw):
    return Settings(**kw)


def test_search_mixin_embedding_mode_validator_fires():
    with pytest.raises(ValidationError, match="OPENSEARCH_EMBEDDING_MODE"):
        _make(OPENSEARCH_EMBEDDING_MODE="app")


def test_search_mixin_topology_clamp_fires():
    assert _make(OPENSEARCH_CHUNKS_INDEX_SHARDS=-4).OPENSEARCH_CHUNKS_INDEX_SHARDS == 1


def test_search_mixin_reranker_threads_validator_fires():
    with pytest.raises(ValidationError, match="CHAT_RERANKER_TORCH_THREADS"):
        _make(CHAT_RERANKER_TORCH_THREADS="banana")


def test_settings_level_pool_floor_validator_fires():
    assert _make(DB_POOL_SIZE=-9).DB_POOL_SIZE == 1


def test_model_validator_resolves_cross_mixin_defaults():
    s = _make(
        ENVIRONMENT="development",
        POSTGRES_HOST="h",
        POSTGRES_PORT="5432",
        POSTGRES_DB="opentranscribe",
        POSTGRES_APP_USER="",
        DATABASE_URL="",
        MIGRATION_DATABASE_URL="",
        REDIS_HOST="r",
        REDIS_URL="",
        CELERY_BROKER_URL="",
    )
    assert s.DATABASE_URL.endswith("@h:5432/opentranscribe")
    assert s.REDIS_URL.endswith("@r:6379/0") or s.REDIS_URL.endswith("//r:6379/0")
    assert s.CELERY_BROKER_URL == s.REDIS_URL
    assert s.MFA_REQUIRE_REDIS is False and s.DEBUG is True
    assert s.LDAP_USER_SEARCH_FILTER == "(sAMAccountName={username})"
    assert s.S3_REGION and s.BEDROCK_REGION is not None


def test_audit_retention_floor_still_rejected():
    with pytest.raises(ValidationError, match="AUDIT_LOG_RETENTION_DAYS"):
        _make(AUDIT_LOG_RETENTION_DAYS=1)
