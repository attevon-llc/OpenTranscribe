"""Static + hermetic guards for the voiceprint backup coverage fix (issue #658).

The round-trip proof lives in ``tests/integration/test_voiceprint_backup_roundtrip.py``
(real OpenSearch, real embeddings, real ``scripts/common.sh`` functions). This file is the
fast half: it pins the things that can silently regress without any container — the CLI
wiring, the corrected coded default, the shipped-file list, and the digest/manifest logic
inside ``scripts/voiceprint-backup.py`` itself.

Why the wiring needs a static guard at all: ``backup_database`` writing a ``pg_dump`` and
nothing else is *exactly* the shipped behaviour issue #658 reports, and it fails no test —
the dump is valid, the exit code is 0, and the missing half (speaker embeddings, which
PostgreSQL does not store) is invisible until a restore. A detector that fails when the
export call disappears is the only thing that makes deleting it loud.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.unit.test_opentr_restore_safety import extract_function
from tests.unit.test_opentr_restore_safety import first_line_index

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMMON_SH = _REPO_ROOT / "scripts" / "common.sh"
_HELPER = _REPO_ROOT / "scripts" / "voiceprint-backup.py"
_MANIFEST = _REPO_ROOT / "release-manifest.txt"


def _common_source() -> str:
    return _COMMON_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def helper() -> ModuleType:
    """Import ``scripts/voiceprint-backup.py`` (hyphenated, so not a normal import)."""
    spec = importlib.util.spec_from_file_location("voiceprint_backup_helper", _HELPER)
    assert spec and spec.loader, f"could not load {_HELPER}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------------------------
# CLI wiring: the export/import/verify calls must exist, and in the right order.
# ---------------------------------------------------------------------------------------------


def test_backup_database_exports_the_speaker_indices() -> None:
    body = extract_function(_common_source(), "backup_database")
    assert body, "backup_database not found in scripts/common.sh"
    assert "os_export_speaker_indices" in body, (
        "backup_database must export the OpenSearch speaker indices — PostgreSQL stores no "
        "embedding vectors, so a pg_dump alone leaves the deployment's voiceprints with no "
        "recoverable copy anywhere (issue #658)"
    )


def test_restore_database_imports_and_then_verifies_the_speaker_indices() -> None:
    body = extract_function(_common_source(), "restore_database")
    assert body, "restore_database not found in scripts/common.sh"

    import_idx = first_line_index(body, "os_import_speaker_indices")
    verify_idx = first_line_index(body, "os_verify_speaker_restore")
    assert import_idx != -1, "restore_database must import the voiceprint artifact"
    assert verify_idx != -1, (
        "restore_database must VERIFY the voiceprint restore — a restore that silently comes "
        "back with zero voiceprints is the same data loss with extra steps"
    )
    assert import_idx < verify_idx, "the verification must run after the import, not before it"


def test_the_voiceprint_restore_runs_after_the_database_has_been_verified() -> None:
    """Ordering is load-bearing: a failed database replay must not replace live voiceprints."""
    body = extract_function(_common_source(), "restore_database")
    assert body, "restore_database not found in scripts/common.sh"

    pg_verify_idx = first_line_index(body, "pg_verify_custom_restore")
    import_idx = first_line_index(body, "os_import_speaker_indices")
    assert pg_verify_idx != -1, "expected the database verification call in restore_database"
    assert import_idx != -1, "expected the voiceprint import call in restore_database"
    assert pg_verify_idx < import_idx, (
        "the voiceprint import must come after the database replay has been verified"
    )


def test_the_voiceprint_restore_runs_before_the_services_are_restarted() -> None:
    """The app must never come back up over a half-restored speaker plane."""
    body = extract_function(_common_source(), "restore_database")
    assert body, "restore_database not found in scripts/common.sh"

    verify_idx = first_line_index(body, "os_verify_speaker_restore")
    decision_idx = first_line_index(body, "pg_restore_restart_decision")
    assert verify_idx != -1, "expected the voiceprint verification call in restore_database"
    assert decision_idx != -1, "expected the restart decision in restore_database"
    assert verify_idx < decision_idx, (
        "voiceprints must be restored and verified before the restart decision is taken"
    )


def test_restore_database_reports_postgres_opensearch_voiceprint_consistency() -> None:
    """``embedding_count`` is a Postgres number about data that lives only in OpenSearch."""
    body = extract_function(_common_source(), "restore_database")
    assert body, "restore_database not found in scripts/common.sh"
    assert "report_voiceprint_consistency" in body, (
        "restore_database must report whether speaker_profile.embedding_count agrees with what "
        "OpenSearch actually holds — otherwise rows silently claim embeddings that do not exist"
    )


# ---------------------------------------------------------------------------------------------
# The corrected coded default, and the comment that justified the wrong one.
# ---------------------------------------------------------------------------------------------


def test_scheduled_backups_include_opensearch_by_default() -> None:
    from app.core import constants

    assert constants.DEFAULT_BACKUP_INCLUDE_OPENSEARCH is True, (
        "the in-app scheduled backup must cover OpenSearch by default: the speaker indices "
        "hold voiceprints, which are NOT derived from anything in PostgreSQL (issue #658)"
    )


def test_the_derived_rebuildable_justification_is_gone_from_the_constant() -> None:
    """The old comment ('OS is derived/rebuildable') is what made the wrong default look safe.

    Scoped to the whole file on purpose. The first draft of this test looked only at the
    characters *preceding* the constant — and the old justification is a TRAILING comment on
    the assignment line, so it passed against the unfixed tree. A guard whose window excludes
    the thing it guards is the #431 failure mode in miniature.
    """
    source = (_REPO_ROOT / "backend" / "app" / "core" / "constants.py").read_text(encoding="utf-8")
    assert "OS is derived/rebuildable" not in source, (
        "the justification for excluding OpenSearch must not survive the default being fixed — "
        "the speaker indices are the sole copy of the deployment's biometric data"
    )


def test_the_helper_is_shipped_to_self_hosted_installs() -> None:
    manifest = _MANIFEST.read_text(encoding="utf-8")
    paths = [
        line.split("\t")[0].strip()
        for line in manifest.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "scripts/voiceprint-backup.py" in paths, (
        "a production `curl | bash` install downloads only what release-manifest.txt lists; "
        "without this entry backup_database would refuse on every self-hosted deployment"
    )


# ---------------------------------------------------------------------------------------------
# The helper's own logic — stdlib only, so it runs in the fast suite with nothing running.
# ---------------------------------------------------------------------------------------------


def test_the_digest_is_order_independent_but_content_sensitive(helper: ModuleType) -> None:
    documents = [
        ("b", {"embedding": [0.1, 0.2], "profile_name": "Beta"}),
        ("a", {"embedding": [0.3, 0.4], "profile_name": "Alpha"}),
    ]
    reversed_documents = list(reversed(documents))
    assert helper.digest_documents(documents) == helper.digest_documents(reversed_documents), (
        "scroll order is not a property of the data — a digest that depends on it would fail "
        "every restore"
    )

    tampered = [("b", {"embedding": [0.1, 0.2000001], "profile_name": "Beta"}), documents[1]]
    assert helper.digest_documents(tampered) != helper.digest_documents(documents), (
        "a single changed vector component must change the digest, or verification proves nothing"
    )


def test_a_missing_document_changes_the_digest(helper: ModuleType) -> None:
    documents = [("a", {"embedding": [1.0]}), ("b", {"embedding": [2.0]})]
    assert helper.digest_documents(documents) != helper.digest_documents(documents[:1])


def test_read_manifest_refuses_an_empty_or_foreign_artifact(helper: ModuleType) -> None:
    import io

    with pytest.raises(helper.VoiceprintBackupError):
        helper.read_manifest(io.StringIO(""))

    with pytest.raises(helper.VoiceprintBackupError):
        helper.read_manifest(io.StringIO('{"format": "something-else"}\n'))

    with pytest.raises(helper.VoiceprintBackupError):
        helper.read_manifest(
            io.StringIO(json.dumps({"format": helper.FORMAT_NAME, "version": 99}) + "\n")
        )


def test_read_manifest_returns_the_bulk_lines_untouched(helper: ModuleType) -> None:
    import io

    manifest = {"format": helper.FORMAT_NAME, "version": helper.FORMAT_VERSION, "indices": []}
    action = json.dumps({"index": {"_index": "speakers_v4", "_id": "profile_1"}})
    source = json.dumps({"embedding": [0.5]})
    parsed, lines = helper.read_manifest(
        io.StringIO(f"{json.dumps(manifest)}\n{action}\n{source}\n")
    )
    assert parsed["format"] == helper.FORMAT_NAME
    assert lines == [action, source]


def test_restorable_settings_drops_the_read_only_index_metadata(helper: ModuleType) -> None:
    """``PUT /<index>`` rejects uuid/creation_date/provided_name — an unfiltered round-trip fails."""
    live = {
        "index": {
            "number_of_shards": "1",
            "number_of_replicas": "0",
            "knn": "true",
            "uuid": "abc123",
            "creation_date": "1700000000000",
            "provided_name": "speakers_v4",
            "version": {"created": "136387827"},
        }
    }
    kept = helper._restorable_settings(live)["index"]
    assert set(kept) == {"number_of_shards", "number_of_replicas", "knn"}


# ---------------------------------------------------------------------------------------------
# Hermetic bash behaviour (no Docker): a failed export must not leave a partial artifact.
# ---------------------------------------------------------------------------------------------


def _call_common_fn(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", "-c", f'source "{_COMMON_SH}"; {name} "$@"', "--", *args],
        capture_output=True,
        text=True,
    )


def test_voiceprint_artifact_path_shares_one_stem_with_encrypted_and_plain_dumps() -> None:
    plain = _call_common_fn("voiceprint_artifact_path", "./backups/ot_2026.sql")
    encrypted = _call_common_fn("voiceprint_artifact_path", "./backups/ot_2026.sql.gpg")
    assert plain.returncode == 0, plain.stderr
    assert encrypted.returncode == 0, encrypted.stderr
    assert plain.stdout.strip() == "./backups/ot_2026.sql.voiceprints.ndjson"
    assert encrypted.stdout.strip() == plain.stdout.strip(), (
        "restore is handed the .gpg name, backup writes the .sql name — they must resolve to "
        "the same artifact stem or a restore silently finds nothing"
    )


def test_a_failed_export_removes_the_partial_artifact(tmp_path: Path) -> None:
    """A half-written artifact must never be mistaken for a complete backup."""
    out_file = tmp_path / "partial.voiceprints.ndjson"
    # `false` as the exec prefix: it swallows the argv and exits 1, i.e. the export fails
    # after the redirect has already created the output file.
    result = _call_common_fn("os_export_speaker_indices", "false", str(out_file), "speakers")
    assert result.returncode != 0, "a failing exec prefix must fail the export"
    assert not out_file.exists(), (
        "the empty artifact left by the redirect must be removed — an empty file parses as "
        "'this deployment has no voiceprints', which is the failure mode being fixed"
    )
