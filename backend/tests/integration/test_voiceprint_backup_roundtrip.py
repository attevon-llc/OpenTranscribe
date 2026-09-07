"""End-to-end proof that speaker voiceprints survive backup -> wipe -> restore (issue #658).

Speaker voiceprints exist in exactly ONE place: the OpenSearch ``speakers_v*`` indices.
PostgreSQL stores no embedding vectors at all — ``SpeakerProfile`` carries only
``embedding_count`` + ``last_embedding_update`` (``app/models/media.py``) and neither
``Speaker`` nor ``SpeakerCluster`` has a vector column — so before this fix
``./opentr.sh backup`` (a bare ``pg_dump``) left a deployment with **no recoverable copy of
its biometric data**.

This drives the REAL ``scripts/common.sh`` primitives the shipped CLI uses
(``os_export_speaker_indices`` / ``os_import_speaker_indices`` /
``os_verify_speaker_restore``), which in turn pipe the real ``scripts/voiceprint-backup.py``
into the OpenSearch container's own ``python3`` — the exact code path
``backup_database``/``restore_database`` execute, not a re-implementation.

**The central assertion is on CONTENT, not on file existence**: after the index is deleted
outright and restored from the artifact, every embedding vector must come back
component-for-component identical. An artifact that exists and restores nothing is the same
data loss with extra steps, so a control assertion between the wipe and the restore proves
the vectors really were gone.

Safety posture, matching the sibling ``test_opentr_restore_roundtrip.py``:

- A dedicated bridge network with **no published ports**, so the throwaway OpenSearch is
  unreachable from the host and cannot be mistaken for the dev stack's cluster on 5180.
- ``uuid4``-suffixed container/network names.
- The image tag is **parsed from ``docker-compose.yml``**, never hardcoded, so this tracks
  the pinned OpenSearch version.
- Container and network are removed in ``finally`` blocks.

Run directly: ``cd backend && PYTHONPATH=. pytest -m integration
tests/integration/test_voiceprint_backup_roundtrip.py -v``
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMMON_SH = _REPO_ROOT / "scripts" / "common.sh"
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"

_INDEX_BASE = "speakers"
_V4_INDEX = "speakers_v4"
_DIMENSION = 256
_BOOT_TIMEOUT_S = 180.0

# Deterministic, mutually distinguishable vectors — named separately from the documents so
# the tampering test below can take one by name (and so its element type survives, rather
# than being erased to `object` by the heterogeneous document literal).
_ALPHA_VECTOR: list[float] = [round(0.001 * i, 6) for i in range(_DIMENSION)]
_BETA_VECTOR: list[float] = [round(-0.002 * i, 6) for i in range(_DIMENSION)]
_SPEAKER_VECTOR: list[float] = [round(0.5 - 0.003 * i, 6) for i in range(_DIMENSION)]

# Two profile documents and one per-file speaker document. Deterministic so a digest
# mismatch means the data changed, not that the fixture re-rolled.
_SEED_DOCS: dict[str, dict[str, Any]] = {
    "profile_alpha": {
        "document_type": "profile",
        "profile_uuid": "0192f000-0000-7000-8000-00000000a1fa",
        "profile_name": "Alpha",
        "user_id": 1,
        "embedding_count": 4,
        "embedding": _ALPHA_VECTOR,
    },
    "profile_beta": {
        "document_type": "profile",
        "profile_uuid": "0192f000-0000-7000-8000-00000000be7a",
        "profile_name": "Beta",
        "user_id": 1,
        "embedding_count": 2,
        "embedding": _BETA_VECTOR,
    },
    "speaker_7": {
        "document_type": "speaker",
        "speaker_uuid": "0192f000-0000-7000-8000-0000000005ea",
        "speaker_id": 7,
        "user_id": 1,
        "media_file_id": 42,
        "embedding": _SPEAKER_VECTOR,
    },
}


def _run(cmd: list[str], *, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, capture_output=True, text=True, input=stdin_text
    )


def _opensearch_image_tag() -> str:
    """Parse the pinned OpenSearch image out of docker-compose.yml — never hardcoded."""
    compose = _COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(r"image:\s*(opensearchproject/opensearch:\S+)", compose)
    assert match, "could not find an `image: opensearchproject/opensearch:<tag>` line"
    return match.group(1)


def _call_common_fn(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke a real ``scripts/common.sh`` function — the exact code the CLI ships."""
    return _run(["bash", "-c", f'source "{_COMMON_SH}"; {name} "$@"', "--", *args])


def _os_request(container: str, method: str, path: str, body: Any = None) -> dict:
    """Talk to the throwaway cluster through ``docker exec`` (it publishes no ports)."""
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "curl",
        "-sS",
        "-X",
        method,
        f"http://127.0.0.1:9200{path}",
    ]
    stdin_text = None
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        stdin_text = json.dumps(body)
    result = _run(cmd, stdin_text=stdin_text)
    assert result.returncode == 0, f"{method} {path} failed: {result.stderr}"
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _wait_ready(container: str) -> None:
    """Poll the cluster health endpoint — never a bare sleep."""
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        result = _run(
            [
                "docker",
                "exec",
                container,
                "curl",
                "-fsS",
                "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=2s",
            ]
        )
        if result.returncode == 0:
            return
        last = result.stderr or result.stdout
        time.sleep(1.0)
    raise RuntimeError(f"OpenSearch in {container} never became ready: {last}")


@pytest.fixture
def os_container() -> Iterator[str]:
    """A throwaway OpenSearch on a private bridge network with no published ports."""
    suffix = uuid.uuid4().hex[:12]
    net_name = f"ot-vp658-net-{suffix}"
    name = f"ot-vp658-os-{suffix}"
    image = _opensearch_image_tag()

    created_net = _run(["docker", "network", "create", net_name])
    assert created_net.returncode == 0, f"failed to create test network: {created_net.stderr}"
    try:
        started = _run(
            [
                "docker",
                "run",
                "-d",
                "--network",
                net_name,
                "--name",
                name,
                "-e",
                "discovery.type=single-node",
                "-e",
                "DISABLE_SECURITY_PLUGIN=true",
                "-e",
                "bootstrap.memory_lock=false",
                "-e",
                "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m",
                image,
            ]
        )
        assert started.returncode == 0, f"failed to start throwaway opensearch: {started.stderr}"
        try:
            _wait_ready(name)
            yield name
        finally:
            _run(["docker", "rm", "-f", name])
    finally:
        _run(["docker", "network", "rm", net_name])


def _create_speaker_index(container: str) -> None:
    _os_request(
        container,
        "PUT",
        f"/{_V4_INDEX}",
        {
            "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0, "knn": True}},
            "mappings": {
                "properties": {
                    "document_type": {"type": "keyword"},
                    "profile_uuid": {"type": "keyword"},
                    "profile_name": {"type": "keyword"},
                    "speaker_uuid": {"type": "keyword"},
                    "speaker_id": {"type": "integer"},
                    "profile_id": {"type": "integer"},
                    "user_id": {"type": "integer"},
                    "media_file_id": {"type": "integer"},
                    "embedding_count": {"type": "integer"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": _DIMENSION,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                            "parameters": {"ef_construction": 128, "m": 24},
                        },
                    },
                }
            },
        },
    )
    _os_request(container, "PUT", f"/{_V4_INDEX}/_alias/{_INDEX_BASE}")


def _seed(container: str) -> None:
    _create_speaker_index(container)
    for doc_id, source in _SEED_DOCS.items():
        _os_request(container, "PUT", f"/{_V4_INDEX}/_doc/{doc_id}?refresh=true", source)


def _read_embeddings(container: str, index: str = _V4_INDEX) -> dict:
    """Read every document's embedding back out of the live cluster, keyed by doc id."""
    response = _os_request(
        container, "POST", f"/{index}/_search", {"size": 100, "query": {"match_all": {}}}
    )
    return {hit["_id"]: hit["_source"]["embedding"] for hit in response["hits"]["hits"]}


def _exec_prefix(container: str) -> str:
    return f"docker exec -i {container}"


# ---------------------------------------------------------------------------------------------
# The central round-trip.
# ---------------------------------------------------------------------------------------------


def test_voiceprints_survive_backup_wipe_and_restore(os_container: str, tmp_path: Path) -> None:
    """Seed -> export -> DELETE the index -> import -> the vectors are back, byte-for-byte."""
    container = os_container
    _seed(container)

    seeded = _read_embeddings(container)
    assert set(seeded) == set(_SEED_DOCS), (
        f"the fixture did not seed what this test asserts on: {sorted(seeded)}"
    )

    artifact = tmp_path / "backup.sql.voiceprints.ndjson"
    export = _call_common_fn(
        "os_export_speaker_indices", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert export.returncode == 0, f"os_export_speaker_indices failed: {export.stderr}"
    assert artifact.is_file(), "the export produced no artifact"

    manifest = json.loads(artifact.read_text(encoding="utf-8").splitlines()[0])
    assert manifest["total_docs"] == len(_SEED_DOCS), (
        f"the artifact claims {manifest['total_docs']} documents, seeded {len(_SEED_DOCS)}"
    )

    # --- destroy the ONLY copy -----------------------------------------------------------
    _os_request(container, "DELETE", f"/{_V4_INDEX}")

    # Control: without this, a restore that did nothing would still pass the assertion below.
    gone = _run(["docker", "exec", container, "curl", "-fsS", f"http://127.0.0.1:9200/{_V4_INDEX}"])
    assert gone.returncode != 0, "the wipe did not actually delete the speaker index"

    restore = _call_common_fn(
        "os_import_speaker_indices", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert restore.returncode == 0, f"os_import_speaker_indices failed: {restore.stderr}"

    verify = _call_common_fn(
        "os_verify_speaker_restore", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert verify.returncode == 0, f"os_verify_speaker_restore reported a mismatch: {verify.stderr}"

    restored = _read_embeddings(container)
    assert restored == seeded, (
        "the restored embeddings are not identical to the ones backed up — a voiceprint that "
        "comes back changed is a profile that silently stops matching"
    )

    alias = _os_request(container, "GET", f"/_alias/{_INDEX_BASE}")
    assert _V4_INDEX in alias, (
        f"the read alias {_INDEX_BASE} was not restored onto {_V4_INDEX}: {sorted(alias)}"
    )


# ---------------------------------------------------------------------------------------------
# The must-fire case for the verifier itself (issue #431's lesson).
# ---------------------------------------------------------------------------------------------


def test_verify_fails_when_a_restored_embedding_differs(os_container: str, tmp_path: Path) -> None:
    """A verifier that cannot fail reports a clean restore forever — including an empty one."""
    container = os_container
    _seed(container)

    artifact = tmp_path / "backup.sql.voiceprints.ndjson"
    export = _call_common_fn(
        "os_export_speaker_indices", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert export.returncode == 0, f"os_export_speaker_indices failed: {export.stderr}"

    baseline = _call_common_fn(
        "os_verify_speaker_restore", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert baseline.returncode == 0, (
        f"the control arm must pass against the cluster it was exported from: {baseline.stderr}"
    )

    # Change ONE component of ONE vector — the smallest corruption that matters.
    tampered = list(_ALPHA_VECTOR)
    tampered[0] = tampered[0] + 0.5
    mutated = dict(_SEED_DOCS["profile_alpha"], embedding=tampered)
    _os_request(container, "PUT", f"/{_V4_INDEX}/_doc/profile_alpha?refresh=true", mutated)

    tampered_verify = _call_common_fn(
        "os_verify_speaker_restore", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert tampered_verify.returncode != 0, (
        "os_verify_speaker_restore must FAIL when a live embedding differs from the backup"
    )
    assert "digest mismatch" in tampered_verify.stderr, (
        f"expected the mismatch to be named, got: {tampered_verify.stderr!r}"
    )


def test_verify_fails_when_the_index_came_back_empty(os_container: str, tmp_path: Path) -> None:
    """The exact silent-failure shape: a restore that reports success with zero voiceprints."""
    container = os_container
    _seed(container)

    artifact = tmp_path / "backup.sql.voiceprints.ndjson"
    export = _call_common_fn(
        "os_export_speaker_indices", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert export.returncode == 0, f"os_export_speaker_indices failed: {export.stderr}"

    _os_request(
        container,
        "POST",
        f"/{_V4_INDEX}/_delete_by_query?refresh=true",
        {"query": {"match_all": {}}},
    )
    assert _read_embeddings(container) == {}, "the emptying step did not empty the index"

    verify = _call_common_fn(
        "os_verify_speaker_restore", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert verify.returncode != 0, (
        "a present-but-empty speaker index must fail verification, not pass it"
    )


# ---------------------------------------------------------------------------------------------
# A fresh install has no speaker indices at all — that must not look like a failure.
# ---------------------------------------------------------------------------------------------


def test_export_of_a_cluster_with_no_speaker_indices_is_a_valid_empty_artifact(
    os_container: str, tmp_path: Path
) -> None:
    container = os_container

    artifact = tmp_path / "empty.sql.voiceprints.ndjson"
    export = _call_common_fn(
        "os_export_speaker_indices", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert export.returncode == 0, f"os_export_speaker_indices failed: {export.stderr}"

    manifest = json.loads(artifact.read_text(encoding="utf-8").splitlines()[0])
    assert manifest["total_docs"] == 0
    assert manifest["indices"] == []

    for mode in ("os_import_speaker_indices", "os_verify_speaker_restore"):
        result = _call_common_fn(mode, _exec_prefix(container), str(artifact), _INDEX_BASE)
        assert result.returncode == 0, (
            f"{mode} rejected a legitimately empty artifact: {result.stderr}"
        )


def test_a_truncated_artifact_is_refused_rather_than_read_as_no_voiceprints(
    os_container: str, tmp_path: Path
) -> None:
    """An unreadable artifact and "this deployment has none" must never be the same outcome."""
    container = os_container
    _seed(container)

    artifact = tmp_path / "backup.sql.voiceprints.ndjson"
    export = _call_common_fn(
        "os_export_speaker_indices", _exec_prefix(container), str(artifact), _INDEX_BASE
    )
    assert export.returncode == 0, f"os_export_speaker_indices failed: {export.stderr}"

    truncated = tmp_path / "truncated.ndjson"
    truncated.write_bytes(b"")

    result = _call_common_fn(
        "os_import_speaker_indices", _exec_prefix(container), str(truncated), _INDEX_BASE
    )
    assert result.returncode != 0, "an empty artifact must be refused, not silently accepted"
