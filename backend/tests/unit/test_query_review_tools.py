"""The #1337 measurement tooling must report what it measured (scripts/perf/query-review).

The M1 baseline lost a run to each of these: a quoted ``shared_preload_libraries`` that bricked
Postgres, the wrong OpenAPI path, a stock driver that kept rows in memory and died on the first
slow endpoint, and a summary that gave every ``/api/files`` variant one statement count.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "scripts" / "perf" / "query-review"
spec = importlib.util.spec_from_file_location("qr_drive", TOOLS / "drive.py")
assert spec and spec.loader
drive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drive)


def _call(identity, name, path, status="200", ms="10"):
    return {"identity": identity, "name": name, "path": path, "status": status, "ms": ms}


def _line(route, dbq, ms="5.0"):
    return f"x GET {route} 200 {ms}ms user=3 dbq={dbq}"


def test_dbq_is_attributed_per_call_not_per_route():
    calls = [
        _call("heavy", "files.default", "/api/files"),
        _call("heavy", "files.sort", "/api/files"),
        _call("member", "files.default", "/api/files"),
    ]
    log = drive.parse_access_log(
        [_line("/api/files", 21), _line("/api/files", 24), _line("/api/files", 33)]
    )

    paired = drive.attribute_dbq(calls, log)

    assert [r["dbq"] for r in paired] == [21, 24, 33]


def test_templated_routes_pair_with_their_uuid_paths():
    uid = "123e4567-e89b-12d3-a456-426614174000"
    calls = [_call("heavy", "speakers.cross_media", f"/api/speakers/{uid}/cross-media")]
    log = drive.parse_access_log([_line("/api/speakers/{speaker_uuid}/cross-media", 8)])

    assert drive.attribute_dbq(calls, log)[0]["dbq"] == 8


def test_a_timed_out_call_is_skipped_without_shifting_later_pairs():
    calls = [
        _call("heavy", "slow", "/api/speakers", status="599"),
        _call("heavy", "files", "/api/files"),
    ]
    log = drive.parse_access_log([_line("/api/files", 21)])

    paired = drive.attribute_dbq(calls, log)

    assert [r["dbq"] for r in paired] == [None, 21]


def test_health_probes_are_not_paired():
    log = drive.parse_access_log([_line("/health", 0), _line("/api/files", 4)])

    assert [m["route"] for m in log] == ["/api/files"]


def test_openapi_path_is_the_mounted_one():
    assert drive.OPENAPI_PATH == "/api/openapi.json"
    assert drive.TIMEOUT_STATUS == 599


def test_no_document_quotes_the_preload_list_as_one_string():
    quoted = re.compile(r"shared_preload_libraries\s*=\s*'[^']*,[^']*'")
    docs = [
        ROOT / "docs" / "design" / "1337_query_review_plan.md",
        TOOLS / "README.md",
        *TOOLS.glob("*.sh"),
    ]

    offenders = [str(p) for p in docs if quoted.search(p.read_text())]

    assert not offenders
