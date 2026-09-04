"""Pin for issue #706's first consumer: `ac_diar_engine_verdict` (scripts/release-tests/lib/
api-client.sh) must read the per-file `media_file.diarization_provider` column via the API
(GET /api/files/{uuid}) instead of grepping 30 minutes of worker stdout for the whole
container.

Hermetic and namespace-scoped per `backend/tests/CLAUDE.md`: `docker` and `curl` are FAKE
shims placed first on PATH so the function under test never reaches a real container or the
live stack (issue #693 destroyed 17 live containers exactly by letting a test invoke real
`docker`/compose tooling). No Docker, no network, no live stack — matching
`test_shipped_scripts_p0_regressions.py`'s convention of extracting the real shell function
with `sed` and driving it under `bash -c`.

Cases, matching the new "<verdict>:<source>" contract:
  1. served-by-native  -> diarization_provider="native"   -> "native:db"
  2. served-by-pyannote -> diarization_provider="pyannote" -> "pyannote:db"
  3. column-NULL       -> diarization_provider=null        -> "none:db"
  4. column-absent (old FROM-release stack, pre-#706) with a worker log showing the legacy
     fallback line -> "fallback:log" (the log-grep path, kept ONLY for a stack that cannot
     have the column at all)
  5. column-absent AND no worker container running -> "absent:none"

Case 4/5 are also what proves the log-fallback path still exists and is distinguishable from
a DB-backed verdict — a caller must never be able to confuse "the column said pyannote" with
"an old stack's logs said fallback".

Issue #707 fixes two defects in the ORIGINAL implementation of this function (the one that
shipped with #706) and this file pins the fix:

  Defect 1 (a REQUEST FAILURE must never be treated as "column absent"): before #707, ANY
  curl failure or unparseable JSON body fell into the same `has_column="no"` branch as a
  genuinely old pre-#706 schema, so a single connection blip on a CURRENT stack silently
  downgraded a per-file DB verdict into an unscoped 30-minute log grep. Cases 6-8 below pin
  the fix: a failed curl (case 6) and unparseable JSON (case 7) must both produce the new,
  distinct `error:request` verdict — never `*:log` — while a genuinely old stack (valid JSON,
  key simply absent — case 8, a stronger version of case 4) still reaches the log fallback.

  Defect 2 (a `:log` verdict must never be an unqualified pass on a stack that should carry
  the column) lives in the CALLERS (test-fresh-install.sh / test-upgrade.sh), not in this
  function, and is out of scope for this file — see those scripts' updated `case` blocks.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
API_CLIENT = REPO_ROOT / "scripts" / "release-tests" / "lib" / "api-client.sh"

pytestmark = pytest.mark.skipif(
    not API_CLIENT.exists(), reason="scripts/release-tests/lib/api-client.sh not present"
)


def _extract_function(script: Path, name: str) -> str:
    fn = subprocess.run(
        ["sed", "-n", f"/^{re.escape(name)}() {{/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert fn.strip(), f"{name}() not found in {script}"
    return fn


AC_CURL_SRC = _extract_function(API_CLIENT, "ac_curl")
AC_VERDICT_SRC = _extract_function(API_CLIENT, "ac_diar_engine_verdict")


def _run_verdict(
    tmp_path: Path,
    file_uuid: str,
    curl_body: str,
    curl_exit: int = 0,
    docker_ps_output: str = "",
    docker_logs_output: str = "",
) -> str:
    """Drive the real, extracted `ac_diar_engine_verdict` under bash with fake `curl`/`docker`
    shims placed first on PATH — never real infrastructure tooling.
    """
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()

    curl_body_file = tmp_path / "curl_body.json"
    curl_body_file.write_text(curl_body)

    (fakebin / "curl").write_text(
        f"""#!/bin/bash
cat "{curl_body_file}"
exit {curl_exit}
"""
    )
    (fakebin / "curl").chmod(0o755)

    docker_ps_file = tmp_path / "docker_ps.txt"
    docker_ps_file.write_text(docker_ps_output)
    docker_logs_file = tmp_path / "docker_logs.txt"
    docker_logs_file.write_text(docker_logs_output)

    (fakebin / "docker").write_text(
        f"""#!/bin/bash
case "$1" in
    ps)
        cat "{docker_ps_file}"
        ;;
    logs)
        cat "{docker_logs_file}"
        ;;
    *)
        echo "unsupported fake docker subcommand: $1" >&2
        exit 1
        ;;
esac
"""
    )
    (fakebin / "docker").chmod(0o755)

    script = f"""
set -euo pipefail
export PATH="{fakebin}:$PATH"
export API_BASE="http://fake-backend/api"
export API_TIMEOUT=10

{AC_CURL_SRC}

{AC_VERDICT_SRC}

ac_diar_engine_verdict "{file_uuid}" "opentranscribe-celery-worker" "30m"
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return proc.stdout.strip()


def test_served_by_native_reads_db_column(tmp_path: Path) -> None:
    verdict = _run_verdict(
        tmp_path,
        "file-uuid-1",
        curl_body='{"uuid": "file-uuid-1", "diarization_provider": "native", "status": "completed"}',
    )
    assert verdict == "native:db"


def test_served_by_pyannote_reads_db_column(tmp_path: Path) -> None:
    verdict = _run_verdict(
        tmp_path,
        "file-uuid-2",
        curl_body='{"uuid": "file-uuid-2", "diarization_provider": "pyannote", "status": "completed"}',
    )
    assert verdict == "pyannote:db"


def test_null_column_is_none_db_not_confused_with_absent_column(tmp_path: Path) -> None:
    verdict = _run_verdict(
        tmp_path,
        "file-uuid-3",
        curl_body='{"uuid": "file-uuid-3", "diarization_provider": null, "status": "completed"}',
    )
    assert verdict == "none:db"


def test_absent_column_old_stack_falls_back_to_log_grep(tmp_path: Path) -> None:
    # No diarization_provider key at all -- an old FROM-release API predating #706.
    verdict = _run_verdict(
        tmp_path,
        "file-uuid-4",
        curl_body='{"uuid": "file-uuid-4", "status": "completed"}',
        docker_ps_output="opentranscribe-celery-worker\n",
        docker_logs_output="2026-09-04 worker: falling back to PyAnnote (sidecar unreachable)\n",
    )
    assert verdict == "fallback:log"


def test_absent_column_and_no_worker_container_is_absent_none(tmp_path: Path) -> None:
    verdict = _run_verdict(
        tmp_path,
        "file-uuid-5",
        curl_body='{"uuid": "file-uuid-5", "status": "completed"}',
        docker_ps_output="",  # worker container not running
        docker_logs_output="",
    )
    assert verdict == "absent:none"


def test_failed_request_is_error_request_not_log_fallback(tmp_path: Path) -> None:
    # issue #707 defect 1: curl fails outright (empty body, non-zero exit) on what could be a
    # CURRENT stack -- this must be reported as a distinct, non-silent "error:request" verdict,
    # never routed into the unscoped log-grep fallback (which is legitimate only for a
    # genuinely old, column-absent schema). The worker log below deliberately contains a
    # "native diarization done" line to prove the fix isn't reading it at all here -- the old,
    # buggy implementation would have reported "native:log" from this exact fixture.
    verdict = _run_verdict(
        tmp_path,
        "file-uuid-6",
        curl_body="",
        curl_exit=1,
        docker_ps_output="opentranscribe-celery-worker\n",
        docker_logs_output="2026-09-04 worker: native diarization done in 4.2s: 3 segments, 2 speakers\n",
    )
    assert verdict == "error:request"


def test_unparseable_json_body_is_error_request_not_log_fallback(tmp_path: Path) -> None:
    # issue #707 defect 1: curl SUCCEEDS (exit 0, non-empty body) but the body is not valid
    # JSON -- e.g. an nginx/gateway error page returned with a 200. This is a request/response
    # failure, not a genuinely old schema, and must not fall back to the log grep either.
    verdict = _run_verdict(
        tmp_path,
        "file-uuid-7",
        curl_body="<html>502 Bad Gateway</html>",
        curl_exit=0,
        docker_ps_output="opentranscribe-celery-worker\n",
        docker_logs_output="2026-09-04 worker: native diarization done in 4.2s: 3 segments, 2 speakers\n",
    )
    assert verdict == "error:request"


def test_genuinely_old_schema_with_valid_json_still_falls_back_to_log(tmp_path: Path) -> None:
    # issue #707: the ONLY legitimate trigger for the log fallback is a successfully-parsed
    # response whose object genuinely has no diarization_provider key -- the real old
    # FROM-release case test-upgrade.sh's header documents. This must keep working after the
    # defect-1 fix separates it from a plain request failure.
    verdict = _run_verdict(
        tmp_path,
        "file-uuid-8",
        curl_body='{"uuid": "file-uuid-8", "status": "completed"}',
        curl_exit=0,
        docker_ps_output="opentranscribe-celery-worker\n",
        docker_logs_output="2026-09-04 worker: native diarization done in 4.2s: 3 segments, 2 speakers\n",
    )
    assert verdict == "native:log"
