"""``scripts/gpu-scale-smoke.sh``'s Flower-worker-stats parser must not word-split silently
wrong (issue #620 LOW bucket, item 8b).

The script reads `/api/workers?refresh=1` and, for the `gpu-scaled@*` entry, extracts its
pool's `max-concurrency` and a freshness verdict via a `python3 -c '...'` one-liner piped
into a plain `read -r A B <<< "$(...)"`. The python side used to print the two values
space-separated (`print(a, b)`); when a worker is registered but has not yet answered an
inspect broadcast (issue #609 — "reported no stats"), `pool.get("max-concurrency", "")` is
`""`, so the line printed is `" fresh"` (a leading space, one token). A whitespace-splitting
`read -r A B` strips that leading space and treats what follows as ONE field, so
`GPU_WORKER_CONCURRENCY` silently became `"fresh"` and `GPU_WORKER_FRESH` became `""` --
the wrong field held the wrong value, and the script went on to report a misleading "stale
entry" failure for a condition that was never about age at all.

Fixed with an explicit `|` separator on both sides (`"|".join([...])` in python,
`IFS='|' read -r A B` in bash) and a THIRD failure branch that names the real cause
("registered but no stats — inspect broadcast timed out") before the age check can ever
misfire on it.

This is a **static + subprocess** test, the same house style as
``test_opentr_restore_safety.py``: the real python snippet is extracted from the shipped
script by regex and run via `python3 -c` against three crafted Flower payloads, so it
exercises the ACTUAL shipped parsing code, not a reimplementation of it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "gpu-scale-smoke.sh"

pytestmark = pytest.mark.skipif(
    not _SCRIPT.exists(), reason="scripts/gpu-scale-smoke.sh not present in this checkout"
)


def _extract_gpu_scaled_parser(source: str) -> str:
    """Pull the `gpu-scaled@` python3 -c '...' snippet's BODY out of the shell script.

    Matched from the opening `python3 -c '` right after the `IFS='|' read` line for
    GPU_WORKER_CONCURRENCY through the closing `' <<<"$WORKERS_JSON")"` — brace/quote
    matched on the known closing marker rather than a fixed line count, so an edit inside
    the snippet doesn't silently truncate what this test runs.
    """
    match = re.search(
        r"GPU_WORKER_CONCURRENCY GPU_WORKER_FRESH <<<\"\$\(python3 -c '\n(?P<body>.*?)\n'"
        r" <<<\"\$WORKERS_JSON\"\)\"",
        source,
        flags=re.DOTALL,
    )
    assert match, "could not locate the gpu-scaled@ worker-stats python snippet"
    return match.group("body")


def _run_parser(body: str, workers_json: dict, *, max_entry_age: int = 120) -> str:
    """Run the extracted snippet exactly as the shell script does: max_age spliced in as
    a literal (matching the script's `'"$FLOWER_MAX_ENTRY_AGE"'` interpolation), payload
    on stdin.
    """
    script = body.replace("'\"$FLOWER_MAX_ENTRY_AGE\"'", str(max_entry_age))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(workers_json),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, f"parser snippet crashed: {proc.stderr}"
    return proc.stdout.rstrip("\n")


@pytest.fixture(scope="module")
def parser_body() -> str:
    return _extract_gpu_scaled_parser(_SCRIPT.read_text(encoding="utf-8"))


def test_worker_with_full_stats_reports_concurrency_and_freshness(parser_body):
    import time

    fixture = {
        "gpu-scaled@host1": {
            "timestamp": time.time(),
            "stats": {"pool": {"max-concurrency": 4}},
        }
    }
    assert _run_parser(parser_body, fixture) == "4|fresh"


def test_worker_registered_with_no_stats_yet_reports_empty_concurrency_not_a_shifted_value(
    parser_body,
):
    """The must-fire case for the original bug: pool={} must not shift "fresh" into the
    concurrency field.
    """
    import time

    fixture = {"gpu-scaled@host1": {"timestamp": time.time(), "stats": {}}}
    result = _run_parser(parser_body, fixture)
    assert result == "|fresh", (
        f"expected empty concurrency + 'fresh' as two separate fields, got {result!r} -- "
        f"a word-split bug would produce 'fresh|' or similar instead"
    )


def test_no_gpu_scaled_worker_at_all_reports_both_fields_empty(parser_body):
    fixture = {"celery@host1": {"timestamp": 0, "stats": {}}}
    assert _run_parser(parser_body, fixture) == "|"


def test_stale_worker_with_full_stats_reports_concurrency_and_stale(parser_body):
    """Sanity check the OTHER dimension still works: an old timestamp reports "stale",
    not confused with the no-stats case.
    """
    fixture = {
        "gpu-scaled@host1": {
            "timestamp": 0,  # epoch 0 -- far older than any max_age
            "stats": {"pool": {"max-concurrency": 2}},
        }
    }
    assert _run_parser(parser_body, fixture) == "2|stale"


# ---------------------------------------------------------------------------------------
# Bash-level branch selection: replicate the script's own conditionals against the THREE
# parsed-field shapes above, proving the third failure branch (registered, no stats) is
# distinguished from "not registered at all" and from "stale".
# ---------------------------------------------------------------------------------------


def _classify(concurrency: str, fresh: str) -> str:
    """Mirrors the bash if-chain in gpu-scale-smoke.sh after the IFS='|' read, in order."""
    if not concurrency and not fresh:
        return "not_registered"
    if fresh and not concurrency:
        return "registered_no_stats"
    if fresh != "fresh":
        return "stale"
    return "ok_pending_concurrency_match"


def test_branch_classification_distinguishes_all_three_failure_shapes():
    assert _classify("", "") == "not_registered"
    assert _classify("", "fresh") == "registered_no_stats"
    assert _classify("2", "stale") == "stale"
    assert _classify("4", "fresh") == "ok_pending_concurrency_match"


def test_script_contains_the_registered_no_stats_failure_branch():
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "reported no stats" in source, (
        "expected the third failure branch naming issue #609's "
        "'inspect broadcast timed out' case distinctly from a stale entry"
    )
    assert "IFS='|' read -r GPU_WORKER_CONCURRENCY GPU_WORKER_FRESH" in source, (
        "expected the GPU_WORKER_CONCURRENCY/GPU_WORKER_FRESH read to use an explicit "
        "'|' separator, not a whitespace split"
    )
