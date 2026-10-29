"""The v0.6.0 feature checks of the release rehearsal are wired in and can actually fail (task #14).

``scripts/release-tests/lib/feature-checks.sh`` holds the checks, ``lib/feature-checks.py`` every
pass/fail decision. Nothing here runs a scenario (they need the live stack stopped and hours); it
pins the three things that rot silently:

1. each scenario driver sources the lib, calls ``fc_run_all`` from its own phase with the intended
   mode, in the intended order relative to the summary and the rollback tail;
2. every ``fc_check_*`` function is defined, listed in exactly one plan array (a check nobody runs
   is a check that cannot fail) and described in the README;
3. every decision predicate returns FAIL on a deliberately bad input and PASS on a good one, and the
   shell self-test (an in-memory backend model with one defect injected at a time) reports that every
   check fires.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[3]
RT = REPO / "scripts" / "release-tests"
LIB_SH = RT / "lib" / "feature-checks.sh"
LIB_PY = RT / "lib" / "feature-checks.py"
FRESH = RT / "test-fresh-install.sh"
UPGRADE = RT / "test-upgrade.sh"
LITE = RT / "test-lite-mode.sh"
README = RT / "README.md"
MATRIX = REPO / "docs-site" / "docs" / "developer-guide" / "full-test-matrix.md"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not LIB_SH.exists(), reason="release-tests not present in this checkout"),
]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("feature_checks_under_test", LIB_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fc = _load()
LIB_TEXT = LIB_SH.read_text(encoding="utf-8")


def _array(name: str) -> list[str]:
    match = re.search(rf"^{name}=\(\s*(.*?)\s*\)", LIB_TEXT, re.MULTILINE | re.DOTALL)
    assert match, f"{name} array not found in feature-checks.sh"
    return match.group(1).split()


# --------------------------------------------------------------------------- wiring


def _driver(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _order(text: str, *needles: str) -> list[int]:
    positions = [text.find(n) for n in needles]
    assert all(p >= 0 for p in positions), dict(zip(needles, positions, strict=True))
    return positions


@pytest.mark.parametrize(
    ("script", "phase_line", "mode", "before", "after"),
    [
        (
            FRESH,
            "phase 07 phase_07_feature_checks",
            "full",
            "phase 06 phase_06_api_smoke",
            "phase 08 phase_08_summary",
        ),
        (
            UPGRADE,
            "phase 11b phase_11b_feature_checks",
            "upgrade",
            "phase 11 phase_11_new_data_post_upgrade",
            "phase 12 phase_12_assert_rollback_precondition",
        ),
        (
            LITE,
            "phase 07b phase_07b_feature_checks",
            "lite",
            "phase 07 phase_07_pipeline_assertions",
            "phase 07c phase_07c_summary",
        ),
    ],
    ids=["fresh-install", "upgrade", "lite"],
)
def test_each_scenario_runs_the_feature_phase_between_its_neighbours(
    script: Path, phase_line: str, mode: str, before: str, after: str
) -> None:
    text = _driver(script)
    assert 'source "$LIB_DIR/feature-checks.sh"' in text, f"{script.name} does not source the lib"
    first, second, third = _order(text, before, phase_line, after)
    assert first < second < third, f"{script.name}: feature phase is out of order"
    assert f"fc_run_all {mode}" in text, f"{script.name}: expected `fc_run_all {mode}`"


def test_upgrade_feature_phase_precedes_the_irreversible_rollback_tail() -> None:
    text = _driver(UPGRADE)
    assert text.find("phase 11b phase_11b_feature_checks") < text.find(
        "phase 13 phase_13_stage_rollback_tree"
    )
    body = text[text.index("phase_11b_feature_checks() {") :]
    guard = body[: body.index("fc_run_all upgrade")]
    assert "ONLY_ROLLBACK == 1" in guard and "return 0" in guard, (
        "phase 11b must skip itself under --only-rollback (the purge belongs to the forward path)"
    )
    # The per-speaker display names must be captured by BOTH snapshots (before and after).
    assert text.count("fc_snapshot_speaker_names") >= 1
    assert "snapshots/before/speaker_display_names.txt" in text
    assert "snapshots/after/speaker_display_names.txt" in text


@pytest.mark.parametrize("script", [FRESH, UPGRADE, LITE], ids=lambda p: p.name)
def test_summary_is_still_the_last_assertion_phase_and_exit_code_propagates(script: Path) -> None:
    text = _driver(script)
    assert text.rstrip().endswith('exit "${RELEASE_TEST_EXIT_CODE:-0}"')
    # No assertion phase may run after the one that prints the verdict.
    summary_idx = max(
        text.find("phase 08 phase_08_summary"),
        text.find("phase 07c phase_07c_summary"),
        text.find("phase 18 phase_18_summary"),
    )
    assert summary_idx > text.find("feature_checks"), (
        f"{script.name}: summary precedes the feature phase"
    )


# --------------------------------------------------------------------------- the lib's own shape


def test_every_check_function_is_defined_planned_once_and_documented() -> None:
    plan = (
        _array("FC_CHECKS_COMMON")
        + _array("FC_CHECKS_UPGRADE")
        + _array("FC_CHECKS_GPU")
        + _array("FC_CHECKS_STATEFUL")
    )
    defined = set(re.findall(r"^(fc_check_\w+)\(\)", LIB_TEXT, re.MULTILINE))
    assert set(plan) == defined, (
        f"defined-but-never-run or planned-but-undefined: {set(plan) ^ defined}"
    )
    assert len(plan) == len(set(plan)), "a check is planned twice"
    readme = README.read_text(encoding="utf-8")
    missing = [name for name in plan if name not in readme]
    assert not missing, f"README.md does not describe: {missing}"


def test_lib_never_pipes_into_an_early_exiting_reader() -> None:
    # scripts/CLAUDE.md: `| grep -q` / `| head -N` under pipefail inverts or aborts.
    code = "\n".join(line for line in LIB_TEXT.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\|\s*(grep\s+-[a-z]*[qm]|head\b|sed\s+[^|]*q)", code)


def test_every_predicate_the_shell_calls_exists() -> None:
    # `fc_judge "label" \<newline> predicate ...` is the common multi-line shape; the label
    # may be followed by a line continuation before the predicate name.
    called = set(
        re.findall(
            r"\b(?:fc_judge\s+\"[^\"]*\"\s*(?:\\\s*\n\s*)?|fc_eval\s+)([a-z][a-z0-9-]+)", LIB_TEXT
        )
    )
    assert called, "found no predicate calls to check"
    # The multi-line calls the single-line regex used to miss must now be seen.
    assert {"head", "display-names", "purge-status", "post-processing"} <= called
    # Every predicate added for the rehearsal gaps (privacy on real OpenSearch, reranker cap, GPU
    # pipeline, populated upgrade) is called through one of the multi-line shapes as well.
    assert {
        "reranker-cap", "os-labels", "drift-scan", "os-seed", "os-docs",
        "purge-residual-complete", "upgrade-counts", "backfill-consistent", "v448-tasks",
        "v448-guard", "gpu-probe", "pipeline-evidence", "worker-log-clean",
    } <= called  # fmt: skip
    assert called <= set(fc.PREDICATES), (
        f"shell calls unknown predicate(s): {called - set(fc.PREDICATES)}"
    )


# --------------------------------------------------------------------------- predicates: must pass AND must fire

GOOD_PURGE = {
    "task_id": "t",
    "status": "completed",
    "counts": {"postgres": {"speaker_identities_reverted": 2}, "opensearch": {"x": 0}},
    "residual": {},
    "residual_ok": True,
    "residual_unverified": [],
    "audit": [{"event_type": "privacy.voiceprints.purge", "task_id": "t"}],
}
GOOD_PP = {
    "file_id": "f",
    "run_at": 1.0,
    "tasks": {
        "search_indexing": {"state": "done", "at": 2.0, "started_at": 1.5},
        "speaker_attributes": {"state": "skipped", "at": 2.0},
    },
}


def _with(base: dict, **patch: object) -> dict:
    return {**base, **patch}


def _verdict(name: str, *args: str) -> int:
    code: int = fc.PREDICATES[name](*args)[0]
    return code


def _j(obj: object) -> str:
    return json.dumps(obj)


FILE_SHOWN = {
    "speakers": [{"uuid": "s1", "display_name": "N", "identity": {"source": "voiceprint"}}]
}
FILE_HIDDEN = {"speakers": [{"uuid": "s1", "display_name": None, "identity": None}]}


def _rerank(threads: int | None, source: str, setting: str) -> str:
    row = {"threads": threads, "source": source, "setting": setting}
    return _j({"system": {"chat_reranker_threads": row}})


_RERANK_AUTO = _rerank(4, "auto", "auto")
_ZERO_RESIDUAL = {k: 0 for k in fc.PURGE_RESIDUAL_KEYS}
_PRE_COUNTS = "media_file=2\ntranscript_segment=5\nspeaker=3\ntask=4\n"
_V448_OK = (
    "rehearsal-v448-A|failed|superseded by rehearsal-v448-B\nrehearsal-v448-B|in_progress|\n"
    "rehearsal-v448-C|failed|superseded by rehearsal-v448-D\nrehearsal-v448-D|completed|\n"
)
_IDX = (
    "CREATE UNIQUE INDEX uq_task_active_per_file_type ON public.task USING btree "
    "(media_file_id, task_type) WHERE (status = ANY (ARRAY['pending', 'in_progress']))"
)
_GPU_OK = {
    "cuda_available": True,
    "device_name": "RTX 3080 Ti",
    "torchcodec_import": "ok",
    "torchaudio_import": "ok",
    "audio_samples": 16000,
    "app_loader_pulled_torchcodec": False,
}


def _drift(expected: list[str], indexed: list[str], drifted: int) -> str:
    return _j({"scanned": 1, "drifted": drifted, "expected": expected, "indexed": indexed})


CASES: list[tuple[str, tuple[str, ...], int]] = [
    # (predicate, args, expected verdict). 0 = pass, 1 = fail, 2 = skip
    ("head", ("v449_x", "v449_x", "449"), 0),
    ("head", ("v448_x", "v449_x", "449"), 1),  # database behind the graph
    ("head", ("v300_x", "v300_x", "449"), 1),  # graph itself older than the floor
    ("head", ("", "", "449"), 1),
    (
        "columns",
        (
            "identity_source\nidentity_source_at\nidentity_source_user_id\nidentity_source_detail\nsuggestion_detail",
        ),
        0,
    ),
    ("columns", ("identity_source\nidentity_source_at",), 1),
    (
        "backfill-record",
        (_j({"revision": "v449", "stamped": 0, "shape_left_unmarked": 0, "recorded_at": "x"}),),
        0,
    ),
    ("backfill-record", ("",), 1),
    ("backfill-record", (_j({"revision": "v449"}),), 1),
    ("display-names", ("1|A\n2|<null>", "1|A\n2|<null>"), 0),
    ("display-names", ("1|A\n2|<null>", "1|B\n2|<null>"), 1),
    ("display-names", ("1|A\n2|<null>", "1|A"), 1),
    ("display-names", ("", "1|A"), 1),  # nothing existed before: nothing proven
    ("display-names", ("(unreadable)", "1|A"), 2),
    (
        "privacy-policy",
        (_j({"voiceprint_matching_enabled": True, "gender_inference_enabled": False}),),
        0,
    ),
    (
        "privacy-policy",
        (_j({"voiceprint_matching_enabled": "yes", "gender_inference_enabled": False}),),
        1,
    ),
    (
        "privacy-policy",
        (_j({"voiceprint_matching_enabled": True, "gender_inference_enabled": False}), "", "true"),
        1,
    ),
    (
        "effective",
        (
            _j(
                {
                    "organization_uuid": None,
                    "voiceprint_matching": {"enabled": True, "locked_by": None},
                    "gender_inference": {"enabled": False, "locked_by": "instance"},
                }
            ),
        ),
        0,
    ),
    (
        "effective",
        (
            _j(
                {
                    "organization_uuid": None,
                    "voiceprint_matching": {"enabled": True, "locked_by": "user"},
                    "gender_inference": {"enabled": True, "locked_by": None},
                }
            ),
        ),
        1,
    ),
    (
        "effective",
        (
            _j(
                {
                    "organization_uuid": "u",
                    "voiceprint_matching": {"enabled": True, "locked_by": None},
                    "gender_inference": {"enabled": True, "locked_by": None},
                }
            ),
        ),
        1,
    ),
    ("speaker-name", (_j(FILE_SHOWN), "s1", "N", "visible"), 0),
    ("speaker-name", (_j(FILE_HIDDEN), "s1", "N", "visible"), 1),
    ("speaker-name", (_j(FILE_HIDDEN), "s1", "N", "hidden"), 0),
    ("speaker-name", (_j(FILE_SHOWN), "s1", "N", "hidden"), 1),
    ("speaker-name", (_j(FILE_SHOWN), "other", "N", "visible"), 1),
    ("purge-status", (_j(GOOD_PURGE), "1"), 0),
    ("purge-status", (_j(_with(GOOD_PURGE, status="failed")), "1"), 1),
    ("purge-status", (_j(_with(GOOD_PURGE, residual_ok=False)), "1"), 1),
    ("purge-status", (_j(_with(GOOD_PURGE, counts={"postgres": {}})), "1"), 1),
    ("purge-status", (_j(_with(GOOD_PURGE, audit=[])), "1"), 1),
    ("purge-status", (_j(_with(GOOD_PURGE, residual_unverified=["x"])), "1"), 1),
    ("purge-status", (_j(GOOD_PURGE), "5"), 1),  # fewer reverted than the seed guarantees
    ("purge-terminal", (_j({"status": "in_progress"}),), 1),
    ("purge-terminal", (_j({"status": "completed"}),), 0),
    ("audit-search", (_j({"logs": [], "total": 1}),), 0),
    ("audit-search", (_j({"logs": [], "total": 0}),), 1),
    (
        "audit-search",
        (_j({"error": "requires AUDIT_LOG_TO_OPENSEARCH", "logs": [], "total": 0}),),
        2,
    ),
    (
        "sse-ready",
        ('event: progress\ndata: {}\n\nevent: ready\ndata: {"url": "http://x/y"}\n\n',),
        0,
    ),
    ("sse-ready", ('event: error\ndata: {"code": "boom"}\n\n',), 1),
    ("sse-ready", ("event: progress\ndata: {}\n\n",), 1),
    ("post-processing", (_j(GOOD_PP), "2", "2", "10"), 0),
    ("post-processing", (_j(_with(GOOD_PP, run_at=None)), "2", "2", "10"), 1),
    (
        "post-processing",
        (
            _j(_with(GOOD_PP, tasks={"speaker_attributes": {"state": "failed", "at": 1}})),
            "0",
            "0",
            "",
        ),
        1,
    ),
    (
        "post-processing",
        (
            _j(_with(GOOD_PP, tasks={"speaker_attributes": {"state": "started", "at": 1}})),
            "0",
            "0",
            "",
        ),
        1,
    ),
    (
        "post-processing",
        (_j(_with(GOOD_PP, tasks={"search_indexing": {"state": "done", "at": 1}})), "2", "2", "10"),
        1,
    ),  # no Gender row
    # attributes existed (10 <= started 20) yet the Gender row ran: the #1313 regression
    (
        "post-processing",
        (
            _j(
                _with(
                    GOOD_PP,
                    tasks={"speaker_attributes": {"state": "done", "at": 30, "started_at": 20}},
                )
            ),
            "2",
            "2",
            "10",
        ),
        1,
    ),
    # detection itself wrote them (50 > started 20): legitimate, but the branch is not exercised
    (
        "post-processing",
        (
            _j(
                _with(
                    GOOD_PP,
                    tasks={"speaker_attributes": {"state": "done", "at": 60, "started_at": 20}},
                )
            ),
            "2",
            "2",
            "50",
        ),
        2,
    ),
    ("pp-settled", (_j(GOOD_PP),), 0),
    ("pp-settled", (_j(_with(GOOD_PP, tasks={"a": {"state": "queued", "at": 1}})),), 1),
    ("policy-still-enabled", (_j({"detail": {"code": "policy_still_enabled"}}),), 0),
    ("policy-still-enabled", (_j({"detail": "nope"}),), 1),
    ("reranker-cap", (_j({"system": {}}), "auto"), 1),  # row absent
    ("reranker-cap", (_RERANK_AUTO, "auto"), 0),
    ("reranker-cap", (_RERANK_AUTO, ""), 0),  # unset env is the default, auto
    ("reranker-cap", (_RERANK_AUTO, "6"), 1),  # the process disagrees with the container env
    ("reranker-cap", (_rerank(6, "configured", "6"), "6"), 0),
    ("reranker-cap", (_rerank(4, "configured", "6"), "6"), 1),  # setting 6 applied as 4
    ("reranker-cap", (_rerank(0, "auto", "auto"), "auto"), 1),  # a zero cap is no cap
    ("reranker-cap", (_rerank(64, "auto", "auto"), "auto"), 1),  # above the auto ceiling
    ("reranker-cap", (_rerank(None, "torch_env", "auto"), "auto"), 0),
    ("reranker-cap", (_rerank(8, "torch_env", "auto"), "auto"), 1),
    ("os-labels", (_j({"labels": ["N"], "indexed": True}), "N", "present"), 0),
    ("os-labels", (_j({"labels": ["x"], "indexed": True}), "N", "present"), 1),
    ("os-labels", (_j({"labels": ["x"], "indexed": True}), "N", "absent"), 0),
    ("os-labels", (_j({"labels": ["N"], "indexed": True}), "N", "absent"), 1),
    (
        "os-labels",
        (_j({"labels": [], "indexed": False}), "N", "absent"),
        1,
    ),  # no chunks proves nothing
    ("os-labels", (_j({"error": "boom"}), "N", "absent"), 1),
    ("drift-scan", (_drift(["N"], ["x"], 1), "N", "drift"), 0),
    ("drift-scan", (_drift(["N"], ["N"], 0), "N", "drift"), 1),  # a blind scan
    ("drift-scan", (_drift(["N"], ["N"], 0), "N", "clean"), 0),
    ("drift-scan", (_drift(["N"], ["x"], 1), "N", "clean"), 1),
    ("os-seed", (_j({"seeded": ["v4:created"], "decoy": "d"}),), 0),
    ("os-seed", (_j({"seeded": [], "decoy": ""}),), 1),
    ("os-docs", (_j({"counts": {"s": 1, "d": 1}}), "s", "d", "before"), 0),
    ("os-docs", (_j({"counts": {"s": 0, "d": 1}}), "s", "d", "before"), 1),
    ("os-docs", (_j({"counts": {"s": 0, "d": 1}}), "s", "d", "after"), 0),
    ("os-docs", (_j({"counts": {"s": 2, "d": 1}}), "s", "d", "after"), 1),  # survivor in one index
    ("os-docs", (_j({"counts": {"s": 0, "d": 0}}), "s", "d", "after"), 1),  # over-broad delete
    ("os-docs", (_j({"counts": {"s": None, "d": 1}}), "s", "d", "after"), 1),  # unmeasured != empty
    ("purge-residual-complete", (_j(_with(GOOD_PURGE, residual=_ZERO_RESIDUAL)),), 0),
    ("purge-residual-complete", (_j(_with(GOOD_PURGE, residual={"speaker_docs": 0})),), 1),
    (
        "purge-residual-complete",
        (_j(_with(GOOD_PURGE, residual={**_ZERO_RESIDUAL, "cluster_docs": 1})),),
        1,
    ),
    ("upgrade-counts", (_PRE_COUNTS, "media_file=3\ntranscript_segment=9\nspeaker=4\ntask=7\n"), 0),
    ("upgrade-counts", (_PRE_COUNTS, "media_file=2\ntranscript_segment=1\nspeaker=4\ntask=7\n"), 1),
    ("upgrade-counts", ("media_file=0\ntranscript_segment=0\nspeaker=0\ntask=0\n", _PRE_COUNTS), 1),
    ("upgrade-counts", ("", _PRE_COUNTS), 1),
    ("backfill-consistent", (_j({"stamped": 3}), "identity_source_not_null=3\n"), 0),
    ("backfill-consistent", (_j({"stamped": 3}), "identity_source_not_null=0\n"), 1),
    ("backfill-consistent", ("", "identity_source_not_null=0\n"), 1),
    ("v448-tasks", (_V448_OK,), 0),
    ("v448-tasks", (_V448_OK.replace("failed|superseded by rehearsal-v448-B", "pending|"),), 1),
    ("v448-tasks", ("rehearsal-v448-B|in_progress|\n",), 1),  # rows deleted
    ("v448-tasks", ("(unseeded: why)\n",), 1),
    (
        "v448-guard",
        (
            'ERROR: duplicate key value violates unique constraint "uq_task_active_per_file_type"',
            _IDX,
            "0",
        ),
        0,
    ),
    ("v448-guard", ("INSERT 0 1", _IDX, "0"), 1),  # the duplicate was accepted
    ("v448-guard", ("uq_task_active_per_file_type", "", "0"), 1),  # no index
    ("v448-guard", ("uq_task_active_per_file_type", _IDX, "2"), 1),  # duplicates remain
    ("gpu-probe", (_j(_GPU_OK),), 0),
    ("gpu-probe", (_j({**_GPU_OK, "cuda_available": False}),), 1),
    ("gpu-probe", (_j({**_GPU_OK, "app_loader_pulled_torchcodec": True}),), 1),
    # torchcodec is optional (the app never imports it); torchaudio is not.
    ("gpu-probe", (_j({**_GPU_OK, "torchcodec_import": "OSError: libnppicc.so.12"}),), 0),
    ("gpu-probe", (_j({**_GPU_OK, "torchaudio_import": "ImportError: x"}),), 1),
    ("gpu-probe", (_j({"error": "x"}),), 1),
    ("pipeline-evidence", ("completed|12|2|native",), 0),
    ("pipeline-evidence", ("completed|0|2|native",), 1),
    ("pipeline-evidence", ("completed|12|0|native",), 1),
    ("pipeline-evidence", ("completed|12|2|",), 1),
    ("pipeline-evidence", ("",), 1),
    ("worker-log-clean", ("ready\ntask done\n",), 0),
    ("worker-log-clean", ("RuntimeError: Could not load libtorchcodec\n",), 1),
    ("worker-log-clean", ("",), 1),
    ("aws-bucket", (_j({"success": False, "message": "AWS_TRANSCRIBE_BUCKET is not set"}),), 0),
    ("aws-bucket", (_j({"success": False, "message": "connection refused"}),), 1),
    ("aws-bucket", (_j({"success": True, "message": "ok"}),), 1),
]


@pytest.mark.parametrize(
    ("name", "args", "expected"), CASES, ids=[f"{n}-{i}" for i, (n, _, _) in enumerate(CASES)]
)
def test_predicate_decides_as_expected(name: str, args: tuple[str, ...], expected: int) -> None:
    assert _verdict(name, *args) == expected


def test_every_predicate_has_a_passing_and_a_failing_case() -> None:
    passing = {name for name, _, expected in CASES if expected == 0}
    firing = {name for name, _, expected in CASES if expected in (1, 2)}
    # export-file is exercised on real bytes below.
    wanted = set(fc.PREDICATES) - {"export-file"}
    assert wanted
    assert wanted - passing == set(), f"no passing case: {wanted - passing}"
    assert wanted - firing == set(), f"no must-fire case: {wanted - firing}"


@pytest.mark.parametrize(
    ("fmt", "good", "bad"),
    [
        ("docx", b"PK\x03\x04rest", b"<html>"),
        ("pdf", b"%PDF-1.7\n", b"PK\x03\x04"),
        ("json", b'{"a": 1}', b"<html>"),
        ("csv", b"Start Time,End Time,Speaker,Text\n1,2,A,b\n", b"a,b,c\n"),
        ("srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n", b"hello\n"),
        ("vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n", b"hello\n"),
        ("md", b"# Title\n\nbody\n", b"no heading\n"),
        ("txt", b"hello there\n", b"<html>error</html>"),
    ],
)
def test_export_file_checks_the_bytes(tmp_path: Path, fmt: str, good: bytes, bad: bytes) -> None:
    ok, no, empty = tmp_path / "ok", tmp_path / "no", tmp_path / "empty"
    ok.write_bytes(good)
    no.write_bytes(bad)
    empty.write_bytes(b"")
    assert fc.export_file(fmt, str(ok))[0] == 0
    assert fc.export_file(fmt, str(no))[0] == 1
    assert fc.export_file(fmt, str(empty))[0] == 1


def test_cli_maps_verdicts_to_exit_codes_and_survives_malformed_input() -> None:
    def run(*argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(LIB_PY), *argv],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    assert run("reranker-cap", "auto", stdin=_RERANK_AUTO).returncode == 0
    assert run("reranker-cap", "6", stdin=_RERANK_AUTO).returncode == 1
    assert run("reranker-cap", stdin="{}").returncode == 1
    assert run("reranker-cap", stdin="not json").returncode == 1  # not a traceback, not a skip
    assert (
        run("audit-search", stdin=_j({"error": "requires AUDIT_LOG_TO_OPENSEARCH=true"})).returncode
        == 2
    )
    # A usage error must not share SKIP's exit code (2): the shell would record it as a skip.
    assert run("no-such-predicate").returncode == 64


# --------------------------------------------------------------------------- the shell self-test


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_selftest_reports_every_control_behaved() -> None:
    result = subprocess.run(
        ["bash", str(RT / "selftest-feature-checks.sh")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    # Not attributable unless it names the cases it ran (stash-window lesson, CLAUDE.md).
    assert "must-fire: defect 'no-hide'" in out and "must-fire: defect 'purge-residual'" in out
    assert out.count("must-fire") >= 20


def test_docs_describe_the_phase_honestly() -> None:
    matrix = MATRIX.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    both = (matrix + readme).lower().replace("**", "")
    for needle in ("feature-checks.sh", "phase 11b", "not prove"):
        assert needle in both, f"docs lack {needle!r}"
    assert "feature-checks.sh" in matrix, "the coverage table must name the lib"


# --------------------------------------------------------------------------- recording and resume


def _bash(script: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=cwd,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_an_unknown_predicate_is_recorded_as_a_fail_not_a_skip() -> None:
    result = _bash(
        f"source {RT}/lib/assertions.sh; source {LIB_SH}; "
        'FC_BODY="{}"; fc_judge "typo" no-such-predicate; echo "fail=$as_fail skip=$as_skip"'
    )

    assert "fail=1 skip=0" in result.stdout, result.stdout + result.stderr


def test_audit_search_skips_only_when_audit_to_opensearch_is_off() -> None:
    off = {"error": "Audit log querying requires AUDIT_LOG_TO_OPENSEARCH=true", "total": 0}
    broken = {"error": "An internal error occurred while querying audit logs.", "total": 0}

    assert fc.audit_search(_j(off))[0] == fc.SKIP
    assert fc.audit_search(_j(broken))[0] == fc.FAIL


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_the_summary_counts_failures_recorded_by_an_earlier_process(tmp_path: Path) -> None:
    """A resumed run's summary phase runs in a NEW process; it must still see the earlier FAIL."""
    ledger = tmp_path / "assertions.ledger"
    report = tmp_path / "REPORT.md"
    prelude = (
        f"export TEST_ASSERT_LEDGER={ledger} TEST_REPORT_FILE={report}; "
        f"source {RT}/lib/assertions.sh; "
    )

    _bash(prelude + 'as_record FAIL "recorded before the resume" "boom"')
    result = _bash(prelude + 'as_record PASS "after"; as_summary || echo "rc=$?"')

    assert "1/2 assertions FAILED" in result.stdout, result.stdout
    assert "rc=1" in result.stdout


@pytest.mark.parametrize("script", [FRESH, UPGRADE, LITE], ids=lambda p: p.name)
def test_a_resumed_scenario_never_truncates_the_report_unguarded(script: Path) -> None:
    lines = script.read_text(encoding="utf-8").splitlines()
    truncations = [i for i, line in enumerate(lines) if line.strip() == ': > "$TEST_REPORT_FILE"']
    assert truncations, f"{script.name}: found no report truncation to guard"
    for index in truncations:
        guard = "\n".join(lines[max(0, index - 6) : index])
        assert "DO_FORCE" in guard and "-s" in guard, (
            f"{script.name}:{index + 1} truncates REPORT.md without checking for an existing "
            "report, so a resumed run loses the earlier FAILs"
        )
    assert "TEST_ASSERT_LEDGER" in script.read_text(encoding="utf-8")


def test_every_probe_command_the_shell_sends_exists_in_the_probe_script() -> None:
    # An unknown command would come back as {"error": ...} and FAIL at run time, hours into a
    # rehearsal; this finds the typo in the fast suite.
    sent = set(re.findall(r'fc_probe\s+"\$\w+"\s+([a-z][a-z-]+)', LIB_TEXT))
    probe = (RT / "lib" / "os-probe.py").read_text(encoding="utf-8")
    known = set(re.findall(r'^\s+"([a-z][a-z-]+)": lambda', probe, re.MULTILINE)) | set(
        re.findall(r"^\s+'([a-z][a-z-]+)': lambda", probe, re.MULTILINE)
    )
    assert {"labels", "drift", "seed-docs", "count-docs", "delete-doc", "gpu"} <= sent
    assert sent <= known, f"fc_probe sends command(s) os-probe.py does not know: {sent - known}"
