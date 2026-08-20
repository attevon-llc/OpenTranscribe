"""Injection-identity guard (#461 A5) — the control-baseline safety net for AMI distractors.

Injecting the AMI distractor corpus (``adapters/ami.py``) changes what every retrieval
number MEASURES: the haystack a QMSum query has to be found in. Nothing about
``metrics.json`` records that on its own — a distractor corpus ships no query loader, so
it never appears in the ``corpora`` block a scored run writes — which is exactly the
footgun this module closes: ``runinfo.json`` records every corpus actually present in the
measured index (scored or not), and ``--compare-only`` REFUSES rather than warns when two
committed baselines' identities disagree.

Loaded via ``importlib`` the same way ``test_eval_fusion_arm.py`` loads the CLI half of
``scripts/benchmark_rag.py`` — it lives outside the ``tests.eval`` package tree.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def _benchmark_rag():
    path = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_rag.py"
    spec = importlib.util.spec_from_file_location("benchmark_rag_under_test_identity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeChunkClient:
    """Answers ``fetch_chunks`` for a fixed ``{file_uuid: chunk_count}`` map.

    Real enough for ``index_reader.fetch_chunks``'s one query shape (a ``terms`` filter on
    ``file_uuid``, paged via ``search_after``) without touching OpenSearch — the same
    trade-off ``test_eval_index_reader.py``'s ``ScriptedClient`` makes for the settle rule.
    """

    def __init__(self, present: dict[str, int]) -> None:
        self._present = present

    def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        if body.get("search_after") is not None:
            return {"hits": {"hits": []}}
        requested = body["query"]["bool"]["filter"][0]["terms"]["file_uuid"]
        hits = []
        for file_uuid in requested:
            for chunk_index in range(self._present.get(file_uuid, 0)):
                hits.append(
                    {
                        "_source": {
                            "file_uuid": file_uuid,
                            "chunk_index": chunk_index,
                            "speaker": "S",
                            "start_time": 0.0,
                            "end_time": 1.0,
                        }
                    }
                )
        return {"hits": {"hits": hits}}


def _write_manifest(
    manifest_dir: Path, key: str, version: str, file_uuids_by_meeting: dict[str, str]
) -> None:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {
                "corpus": {
                    "key": key,
                    "name": key,
                    "version": version,
                    "license_tier": "A",
                    "root": str(manifest_dir),
                }
            }
        ),
        encoding="utf-8",
    )
    lines = [
        json.dumps({"meeting_id": meeting_id, "file_uuid": file_uuid, "extra": {}})
        for meeting_id, file_uuid in file_uuids_by_meeting.items()
    ]
    (manifest_dir / "files.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# _fingerprint — pure, order-independent
# --------------------------------------------------------------------------


class TestFingerprint:
    def test_is_deterministic_across_two_independently_built_equal_inputs(self):
        """Two SEPARATE list/dict objects with equal content, not the same reference
        called twice — that would be true by construction regardless of what
        ``_fingerprint`` does with it."""
        module = _benchmark_rag()
        first = [{"key": "qmsum", "version": "abc"}]
        second = [dict(key="qmsum", version="abc")]
        assert first is not second
        assert module._fingerprint(first) == module._fingerprint(second)

    def test_matches_a_fixed_precomputed_digest(self):
        """Pins the actual algorithm (sha256 of canonical JSON, first 16 hex chars) —
        catches a change to the hash function or the truncation length silently
        producing a different-but-still-self-consistent fingerprint."""
        import hashlib
        import json as _json

        module = _benchmark_rag()
        entries = [{"key": "qmsum", "version": "abc"}]
        expected = hashlib.sha256(
            _json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        assert module._fingerprint(entries) == expected

    def test_key_order_inside_an_entry_does_not_matter(self):
        module = _benchmark_rag()
        a = [{"key": "qmsum", "version": "abc"}]
        b = [{"version": "abc", "key": "qmsum"}]
        assert module._fingerprint(a) == module._fingerprint(b)

    def test_a_different_version_changes_the_fingerprint(self):
        module = _benchmark_rag()
        a = [{"key": "qmsum", "version": "abc"}]
        b = [{"key": "qmsum", "version": "def"}]
        assert module._fingerprint(a) != module._fingerprint(b)

    def test_an_extra_corpus_changes_the_fingerprint(self):
        """The AMI-distractor case: same scored corpus, one extra entry present."""
        module = _benchmark_rag()
        qmsum_only = [{"key": "qmsum", "version": "abc"}]
        qmsum_plus_ami = [{"key": "qmsum", "version": "abc"}, {"key": "ami", "version": "1.6.2"}]
        assert module._fingerprint(qmsum_only) != module._fingerprint(qmsum_plus_ami)


# --------------------------------------------------------------------------
# _scan_injection_identity — reads manifests off disk, checks index presence
# --------------------------------------------------------------------------


class TestScanInjectionIdentity:
    def test_only_manifests_with_indexed_files_are_recorded(self, tmp_path: Path):
        module = _benchmark_rag()
        manifest_root = tmp_path / "injections"
        _write_manifest(
            manifest_root / "qmsum", "qmsum", "abc123", {"m1": "uuid-1", "m2": "uuid-2"}
        )
        # A stale manifest on disk whose files never landed in THIS index — must be
        # excluded, not recorded as files_present_in_index: 0.
        _write_manifest(manifest_root / "orphaned", "orphaned", "zzz", {"m9": "uuid-9"})

        client = FakeChunkClient(present={"uuid-1": 3, "uuid-2": 5})
        identity = module._scan_injection_identity(
            client, "chunks-index", manifest_root, scored_keys={"qmsum"}
        )
        keys = [c["key"] for c in identity["corpora"]]
        assert keys == ["qmsum"]

    def test_a_distractor_corpus_is_recorded_as_unscored(self, tmp_path: Path):
        module = _benchmark_rag()
        manifest_root = tmp_path / "injections"
        _write_manifest(manifest_root / "qmsum", "qmsum", "abc123", {"m1": "uuid-1"})
        _write_manifest(manifest_root / "ami", "ami", "1.6.2", {"EN2001a": "uuid-ami-1"})

        client = FakeChunkClient(present={"uuid-1": 3, "uuid-ami-1": 7})
        identity = module._scan_injection_identity(
            client, "chunks-index", manifest_root, scored_keys={"qmsum"}
        )
        by_key = {c["key"]: c for c in identity["corpora"]}
        assert by_key["qmsum"]["scored"] is True
        assert by_key["ami"]["scored"] is False
        assert by_key["ami"]["files_present_in_index"] == 1

    def test_entries_are_sorted_by_key_for_a_stable_fingerprint(self, tmp_path: Path):
        module = _benchmark_rag()
        manifest_root = tmp_path / "injections"
        _write_manifest(manifest_root / "z_corpus", "z_corpus", "1", {"m1": "uuid-z"})
        _write_manifest(manifest_root / "a_corpus", "a_corpus", "1", {"m2": "uuid-a"})
        client = FakeChunkClient(present={"uuid-z": 1, "uuid-a": 1})
        identity = module._scan_injection_identity(
            client, "chunks-index", manifest_root, scored_keys=set()
        )
        assert [c["key"] for c in identity["corpora"]] == ["a_corpus", "z_corpus"]

    def test_a_missing_manifest_root_yields_an_empty_but_valid_identity(self, tmp_path: Path):
        module = _benchmark_rag()
        client = FakeChunkClient(present={})
        identity = module._scan_injection_identity(
            client, "chunks-index", tmp_path / "does-not-exist", scored_keys=set()
        )
        assert identity["corpora"] == []
        assert identity["fingerprint"] == module._fingerprint([])


# --------------------------------------------------------------------------
# _refuse_on_differing_injection_identity — the --compare-only guard
# --------------------------------------------------------------------------


def _baseline_dir(tmp_path: Path, name: str, injection_identity: dict[str, Any] | None) -> Path:
    out = tmp_path / name
    out.mkdir(parents=True)
    (out / "metrics.json").write_text(json.dumps({"control_name": name}), encoding="utf-8")
    if injection_identity is not None:
        (out / "runinfo.json").write_text(
            json.dumps({"injection_identity": injection_identity}), encoding="utf-8"
        )
    return out


class TestRefuseOnDifferingInjectionIdentity:
    def test_identical_identity_is_allowed(self, tmp_path: Path):
        module = _benchmark_rag()
        identity = {"corpora": [{"key": "qmsum"}], "fingerprint": "same-fp"}
        a = _baseline_dir(tmp_path, "a", identity)
        b = _baseline_dir(tmp_path, "b", identity)
        assert module._refuse_on_differing_injection_identity(str(a), str(b)) is None

    def test_a_qmsum_only_run_vs_a_qmsum_plus_ami_run_is_refused(self, tmp_path: Path):
        module = _benchmark_rag()
        qmsum_only = {"corpora": [{"key": "qmsum"}], "fingerprint": "fp-qmsum-only"}
        qmsum_plus_ami = {
            "corpora": [{"key": "ami"}, {"key": "qmsum"}],
            "fingerprint": "fp-qmsum-plus-ami",
        }
        a = _baseline_dir(tmp_path, "a", qmsum_only)
        b = _baseline_dir(tmp_path, "b", qmsum_plus_ami)
        refusal = module._refuse_on_differing_injection_identity(str(a), str(b))
        assert refusal is not None
        assert "DIFFERENT" in refusal

    def test_both_missing_runinfo_warns_but_does_not_refuse(self, tmp_path: Path):
        """Every baseline committed before #461 A5 has no injection_identity at all —
        refusing every legacy comparison would be a regression, not a safety win."""
        module = _benchmark_rag()
        a = _baseline_dir(tmp_path, "a", None)
        b = _baseline_dir(tmp_path, "b", None)
        assert module._refuse_on_differing_injection_identity(str(a), str(b)) is None

    def test_one_missing_runinfo_also_warns_rather_than_refuses(self, tmp_path: Path):
        module = _benchmark_rag()
        identity = {"corpora": [{"key": "qmsum"}], "fingerprint": "fp"}
        a = _baseline_dir(tmp_path, "a", identity)
        b = _baseline_dir(tmp_path, "b", None)
        assert module._refuse_on_differing_injection_identity(str(a), str(b)) is None

    def test_refusal_names_both_baselines_and_their_corpora(self, tmp_path: Path):
        module = _benchmark_rag()
        identity_a = {"corpora": [{"key": "qmsum"}], "fingerprint": "fp-a"}
        identity_b = {"corpora": [{"key": "qmsum"}, {"key": "ami"}], "fingerprint": "fp-b"}
        a = _baseline_dir(tmp_path, "control-a", identity_a)
        b = _baseline_dir(tmp_path, "control-b", identity_b)
        refusal = module._refuse_on_differing_injection_identity(str(a), str(b))
        assert refusal is not None
        assert "control-a" in refusal
        assert "control-b" in refusal


# --------------------------------------------------------------------------
# End-to-end at the CLI seam: --compare-only actually refuses (exit 3)
# --------------------------------------------------------------------------


class TestCompareOnlyCliRefuses:
    def test_compare_only_exits_3_on_differing_identity(self, tmp_path: Path, monkeypatch):
        module = _benchmark_rag()
        monkeypatch.setattr(module, "DEFAULT_BASELINE_ROOT", tmp_path)
        qmsum_only = {"corpora": [{"key": "qmsum"}], "fingerprint": "fp-qmsum-only"}
        qmsum_plus_ami = {
            "corpora": [{"key": "ami"}, {"key": "qmsum"}],
            "fingerprint": "fp-qmsum-plus-ami",
        }
        # BOTH sides get valid, matching retrieval_per_query rows — otherwise the
        # earlier "no retrieval_per_query rows" guard (checked before the identity
        # refusal) would return 3 for an unrelated reason, and this test would pass
        # without ever exercising the injection-identity check it exists to prove.
        # (Caught by red-checking this exact test: mutating the identity comparison
        # to always allow the run still passed until this fix.)
        retrieval_rows = [
            {"query_id": "q1", "query_class": "c", "corpus": "qmsum", "scores": {"ndcg_10": 0.5}}
        ]
        a = _baseline_dir(tmp_path, "control-a", qmsum_only)
        (a / "metrics.json").write_text(
            json.dumps({"control_name": "control-a", "retrieval_per_query": retrieval_rows}),
            encoding="utf-8",
        )
        b = _baseline_dir(tmp_path, "control-b", qmsum_plus_ami)
        (b / "metrics.json").write_text(
            json.dumps({"control_name": "control-b", "retrieval_per_query": retrieval_rows}),
            encoding="utf-8",
        )

        args = module.build_parser().parse_args(["--compare-only", "control-a", "control-b"])
        assert module._run_compare_only(args) == 3

    def test_compare_only_still_runs_when_neither_baseline_has_an_identity(
        self, tmp_path: Path, monkeypatch
    ):
        module = _benchmark_rag()
        monkeypatch.setattr(module, "DEFAULT_BASELINE_ROOT", tmp_path)
        rows = [
            {
                "query_id": "q1",
                "query_class": "c",
                "corpus": "qmsum",
                "scores": {"ndcg_10": 0.5, "recall_10": 0.5, "mrr_10": 0.5},
            }
        ]
        for name in ("legacy-a", "legacy-b"):
            out = tmp_path / name
            out.mkdir()
            (out / "metrics.json").write_text(
                json.dumps({"control_name": name, "retrieval_per_query": rows}), encoding="utf-8"
            )
        args = module.build_parser().parse_args(["--compare-only", "legacy-a", "legacy-b"])
        assert module._run_compare_only(args) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
