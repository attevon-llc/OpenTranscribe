"""Guard: no script resolves "which stack" by name substring, hardcoded container or live port.

Four detectors over the tracked scripts and the live-stack test helpers. Each finding must be
fixed or sit in ``PENDING`` keyed to the lane that converts it; ``PENDING`` may only shrink (an
entry that no longer fires fails the run) and is not an allowlist for fixed sites. See
scripts/lib/stack_target.py for the library the fixes use.
"""

from __future__ import annotations

import ast
import functools
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIBRARY_MARKERS = ("stack-target.sh", "stack_target")
D1, D2, D3, D4 = "name-substring", "hardcoded-container", "live-port-default", "compose-no-project"

# (path, detector) -> reason naming the lane that converts it. Must reach empty.
PENDING: dict[tuple[str, str], str] = {
    (
        "backend/tests/api/test_ws_origin_gate.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    ("backend/tests/conftest.py", "live-port-default"): "STK2 (the gate)",
    ("backend/tests/e2e/conftest.py", "live-port-default"): "STK3 (e2e and UI)",
    (
        "backend/tests/e2e/idp_overlay.py",
        "compose-no-project",
    ): "P5 item 2 (auth e2e and the IdP overlay)",
    ("backend/tests/e2e/test_pki.py", "live-port-default"): "STK3 (e2e and UI)",
    ("backend/tests/e2e/test_visual_regression.py", "live-port-default"): "STK3 (e2e and UI)",
    ("backend/tests/fixtures/search_corpus_stack.py", "live-port-default"): "STK3 (e2e and UI)",
    (
        "backend/tests/integration/deletion_seed.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_celery_queue_depth_live.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_cleanup_test_data_isolated.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_cleanup_test_users_isolated_db.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_deletion_residue_live.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_diar_native_cross_card_placement_live.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_diar_native_multigpu_provider_live.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_gpu_scale_smoke_live.py",
        "live-port-default",
    ): "STK2 (the gate)",
    (
        "backend/tests/integration/test_lite_mode_mocked_providers.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_media_mirror_live.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_orphaned_delivery_recovery_live.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_presign_revocation.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/integration/test_v436_lite_settings_migration.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/test_auth_config_integration.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/test_embedding_migration_integration.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/test_search_quality.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "backend/tests/test_selective_reprocess.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "scripts/audit_tests/selftest_cases.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    ("scripts/bench_parallel/config.py", "live-port-default"): "P5 item 5 (benchmarks)",
    ("scripts/benchmark-diarization.py", "live-port-default"): "P5 item 5 (benchmarks)",
    ("scripts/benchmark_concurrency_sweep.sh", "hardcoded-container"): "P5 item 5 (benchmarks)",
    ("scripts/benchmark_engine_queue.py", "live-port-default"): "P5 item 5 (benchmarks)",
    ("scripts/benchmark_transcription.sh", "compose-no-project"): "P5 item 5 (benchmarks)",
    ("scripts/benchmark_transcription.sh", "live-port-default"): "P5 item 5 (benchmarks)",
    ("scripts/benchmark_upload_baseline.py", "live-port-default"): "P5 item 5 (benchmarks)",
    (
        "scripts/bulk-processing-cheatsheet.sh",
        "hardcoded-container",
    ): "P5 item 6 (legacy test and ops scripts)",
    (
        "scripts/bulk-processing-cheatsheet.sh",
        "live-port-default",
    ): "P5 item 6 (legacy test and ops scripts)",
    ("scripts/check-schema-drift.py", "live-port-default"): "STK2 (the gate)",
    (
        "scripts/cleanup-test-users.py",
        "live-port-default",
    ): "P5 item 3 (cleanup and auth-config tools)",
    (
        "scripts/cleanup_test_data/cli.py",
        "live-port-default",
    ): "P5 item 3 (cleanup and auth-config tools)",
    (
        "scripts/cleanup_test_data/settings.py",
        "live-port-default",
    ): "P5 item 3 (cleanup and auth-config tools)",
    (
        "scripts/compare-patch-results.sh",
        "live-port-default",
    ): "P5 item 6 (legacy test and ops scripts)",
    (
        "scripts/dev-test-auth-config.py",
        "live-port-default",
    ): "P5 item 3 (cleanup and auth-config tools)",
    (
        "scripts/diagnose-file-integrity.sh",
        "hardcoded-container",
    ): "P5 item 6 (legacy test and ops scripts)",
    ("scripts/e2e/run-e2e.sh", "live-port-default"): "STK3 (e2e and UI)",
    ("scripts/e2e_bench/config.py", "live-port-default"): "P5 item 5 (benchmarks)",
    ("scripts/gpu-profile-test.sh", "hardcoded-container"): "P5 item 5 (benchmarks)",
    ("scripts/gpu-profile-test.sh", "live-port-default"): "P5 item 5 (benchmarks)",
    ("scripts/gpu-scale-smoke.sh", "name-substring"): "STK2 (the gate)",
    (
        "scripts/install-offline-package.sh",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    ("scripts/lite-smoke.sh", "name-substring"): "STK4 (release and rehearsal)",
    (
        "scripts/opentr-offline.sh",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "scripts/pki/generate-test-env.sh",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    ("scripts/pki/run-pki-e2e-leg.sh", "live-port-default"): "STK4 (release and rehearsal)",
    (
        "scripts/pki/test-pki-auth.sh",
        "live-port-default",
    ): "P5 item 6 (legacy test and ops scripts)",
    (
        "scripts/release-tests/lib/feature-checks.sh",
        "live-port-default",
    ): "STK4 (release and rehearsal)",
    (
        "scripts/release-tests/test-cold-model-download.sh",
        "name-substring",
    ): "STK4 (release and rehearsal)",
    (
        "scripts/release-tests/test-fresh-install.sh",
        "hardcoded-container",
    ): "STK4 (release and rehearsal)",
    (
        "scripts/release-tests/test-fresh-install.sh",
        "live-port-default",
    ): "STK4 (release and rehearsal)",
    (
        "scripts/release-tests/test-lite-mode.sh",
        "compose-no-project",
    ): "STK4 (release and rehearsal)",
    (
        "scripts/release-tests/test-lite-mode.sh",
        "hardcoded-container",
    ): "STK4 (release and rehearsal)",
    (
        "scripts/release-tests/test-lite-mode.sh",
        "live-port-default",
    ): "STK4 (release and rehearsal)",
    ("scripts/release-tests/test-lite-mode.sh", "name-substring"): "STK4 (release and rehearsal)",
    (
        "scripts/release-tests/test-upgrade.sh",
        "hardcoded-container",
    ): "STK4 (release and rehearsal)",
    ("scripts/release-tests/test-upgrade.sh", "live-port-default"): "STK4 (release and rehearsal)",
    ("scripts/release/fresh-stack-lib.sh", "live-port-default"): "STK4 (release and rehearsal)",
    ("scripts/run-auth-e2e.sh", "hardcoded-container"): "P5 item 2 (auth e2e and the IdP overlay)",
    ("scripts/run-auth-e2e.sh", "live-port-default"): "P5 item 2 (auth e2e and the IdP overlay)",
    ("scripts/run-auth-e2e.sh", "name-substring"): "P5 item 2 (auth e2e and the IdP overlay)",
    ("scripts/run-dev-tests.sh", "live-port-default"): "STK2 (the gate)",
    ("scripts/run-integration-tests.sh", "live-port-default"): "STK2 (the gate)",
    ("scripts/run-mutation-tests.sh", "live-port-default"): "STK2 (the gate)",
    ("scripts/run-summary-eval.sh", "name-substring"): "STK4 (release and rehearsal)",
    ("scripts/run_benchmark.py", "live-port-default"): "P5 item 5 (benchmarks)",
    ("scripts/run_benchmark.py", "name-substring"): "P5 item 5 (benchmarks)",
    ("scripts/seed-fresh-deployment.sh", "live-port-default"): "P5 item 7 (standalone defaults)",
    (
        "scripts/soak_seed_audio.py",
        "live-port-default",
    ): "P5 item 8 (not in the plan inventory: triage while emptying this list)",
    (
        "scripts/speaker-profiles-backup.sh",
        "live-port-default",
    ): "P5 item 6 (legacy test and ops scripts)",
    ("scripts/test-all-auth.sh", "live-port-default"): "P5 item 6 (legacy test and ops scripts)",
    (
        "scripts/test-download-recovery.sh",
        "hardcoded-container",
    ): "P5 item 6 (legacy test and ops scripts)",
    (
        "scripts/test-download-recovery.sh",
        "live-port-default",
    ): "P5 item 6 (legacy test and ops scripts)",
    ("scripts/test-fedramp.sh", "live-port-default"): "P5 item 6 (legacy test and ops scripts)",
    ("scripts/test-ldap-auth.sh", "live-port-default"): "P5 item 6 (legacy test and ops scripts)",
    (
        "scripts/test-playlist-recovery-fix.sh",
        "hardcoded-container",
    ): "P5 item 6 (legacy test and ops scripts)",
    (
        "scripts/test-search-indexing-recovery.sh",
        "hardcoded-container",
    ): "P5 item 6 (legacy test and ops scripts)",
    (
        "scripts/test-search-indexing-recovery.sh",
        "live-port-default",
    ): "P5 item 6 (legacy test and ops scripts)",
    ("scripts/update-a11y-baseline.py", "live-port-default"): "STK3 (e2e and UI)",
}

# Not defaults for a resolved target; each carries its written reason.
EXEMPT: dict[tuple[str, str], str] = {
    (
        "opentr.sh",
        "live-port-default",
    ): "opentr.sh OWNS the stack: the stock ports are its definition (FRESH_PORT_VARS), not a guess about another stack",
    (
        "scripts/demo/demo-upgrade.sh",
        "live-port-default",
    ): "the demo scripts already assert the stack (health version equals the tag); the plan says leave them",
    (
        "opentranscribe.sh",
        "live-port-default",
    ): "ships standalone to end users: stock ports and container names are its documented install layout, not a developer stack guess",
    (
        "opentranscribe.sh",
        "hardcoded-container",
    ): "ships standalone to end users: stock ports and container names are its documented install layout, not a developer stack guess",
}

# Files whose compose invocations are the stack owners (they derive the project themselves).
D4_OWNERS = ("opentr.sh", "opentranscribe.sh", "scripts/common.sh")


# ----------------------------------------------------------------------------- text extraction


def shell_code(text: str) -> str:
    """Shell source minus comment lines and heredoc bodies, continuations joined."""
    out: list[str] = []
    terminator: str | None = None
    for line in text.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
        match = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        if match:
            terminator = match.group(1)
    return re.sub(r"\\\n", " ", "\n".join(out))


def python_strings(text: str) -> tuple[str, list[ast.List]]:
    """All string constants (docstrings excluded) joined by newlines, and the List nodes."""
    tree = ast.parse(text)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]
    lists = [n for n in ast.walk(tree) if isinstance(n, ast.List)]
    return "\n".join(strings), lists


def _first_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return ""


# ----------------------------------------------------------------------------- detectors

_NAME_FILTER = re.compile(r"--filter[ =]+[\"']?name=(?!\^)")
_HARDCODED = re.compile(
    r"docker\s+(?:exec|logs|inspect|restart|stop|port)\s+(?:-\S+\s+)*[\"']?"
    r"(?:opentranscribe|transcribe-app)-[a-z][a-z-]*"
)
_LIVE_PORT = r"51(?:7[3-9]|8\d|90)"
_PORT_DEFAULT = re.compile(rf"\$\{{[A-Z_]*PORT:-{_LIVE_PORT}\}}")
_LOCAL_PORT = re.compile(rf"(?:localhost|127\.0\.0\.1):{_LIVE_PORT}\b")
_PORT_PROBE = re.compile(rf"\bport_open\s+{_LIVE_PORT}\b")
_PROJECT_MARK = re.compile(r"(?:^|\s)(?:-p|--project-name)[\s=]|COMPOSE_PROJECT_NAME=")


def detect_name_substring(code: str, lists: list[ast.List] | None = None) -> bool:
    if _NAME_FILTER.search(code):
        return True
    for node in lists or []:
        elts = getattr(node, "elts", [])
        for prev, cur in zip(elts, elts[1:], strict=False):
            if _first_text(prev) == "--filter" and _first_text(cur).startswith("name="):
                if not _first_text(cur).startswith("name=^"):
                    return True
    return False


def detect_hardcoded_container(code: str) -> bool:
    return bool(_HARDCODED.search(code))


def detect_live_port_default(code: str, text: str, is_python: bool) -> bool:
    if any(marker in text for marker in LIBRARY_MARKERS):
        return False
    if _PORT_DEFAULT.search(code) or _LOCAL_PORT.search(code) or _PORT_PROBE.search(code):
        return True
    if is_python:
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and len(node.args) >= 2:
                key, default = node.args[0], node.args[1]
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.endswith("PORT")
                    and re.fullmatch(_LIVE_PORT, str(getattr(default, "value", "")))
                ):
                    return True
    return False


def detect_compose_no_project(code: str, lists: list[ast.List] | None = None) -> bool:
    for line in code.splitlines():
        if re.search(r"docker\s+compose\b", line) and re.search(r"\s-f[\s=]", line):
            if not _PROJECT_MARK.search(line):
                return True
    for node in lists or []:
        elts = [_first_text(e) for e in getattr(node, "elts", [])]
        if "docker" in elts and "compose" in elts and ("-f" in elts or "--file" in elts):
            if not ({"-p", "--project-name"} & set(elts)):
                return True
    return False


def findings_for(rel: str, text: str) -> set[str]:
    is_python = rel.endswith(".py")
    if is_python:
        code, lists = python_strings(text)
    else:
        code, lists = shell_code(text), []
    found: set[str] = set()
    if detect_name_substring(code, lists):
        found.add(D1)
    if detect_hardcoded_container(code):
        found.add(D2)
    if detect_live_port_default(code, text, is_python):
        found.add(D3)
    if rel not in D4_OWNERS and detect_compose_no_project(code, lists):
        found.add(D4)
    return found


def stale_entries(
    hits: dict[tuple[str, str], bool], entries: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Entries whose site no longer fires: PENDING and EXEMPT may only shrink."""
    return sorted(k for k in entries if k not in hits)


def tracked_files() -> list[str]:
    out = subprocess.run(
        [
            "git",
            "-C",
            str(REPO),
            "ls-files",
            "scripts",
            "backend/tests",
            "opentr.sh",
            "opentranscribe.sh",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    keep = []
    for rel in out:
        if not rel.endswith((".sh", ".py")) and rel not in ("opentr.sh", "opentranscribe.sh"):
            continue
        if rel.startswith("backend/tests/unit/") or not (REPO / rel).is_file():
            continue
        if rel.startswith("backend/tests/") and not rel.endswith(".py"):
            continue
        keep.append(rel)
    return keep


@functools.cache
def scan() -> dict[tuple[str, str], bool]:
    hits: dict[tuple[str, str], bool] = {}
    for rel in tracked_files():
        try:
            found = findings_for(rel, (REPO / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        hits.update({(rel, detector): True for detector in found})
    return hits


# ----------------------------------------------------------------------------- the guard


def test_every_finding_is_fixed_or_pending_with_a_reason() -> None:
    hits = scan()
    unlisted = sorted(k for k in hits if k not in PENDING and k not in EXEMPT)
    assert not unlisted, (
        "stack-targeting findings (resolve through scripts/lib/stack-target.sh; never by name "
        f"substring, hardcoded container or live-port default): {unlisted}"
    )


def test_pending_only_shrinks_and_every_entry_still_fires() -> None:
    stale = stale_entries(scan(), [*PENDING, *EXEMPT])
    assert not stale, f"entries that no longer fire must be deleted: {stale}"


def test_every_pending_and_exempt_entry_carries_a_reason_and_a_lane() -> None:
    for key, reason in PENDING.items():
        assert reason.strip(), f"{key} has no reason"
        assert re.search(r"\b(?:STK[1-4]|P5 item [1-8])\b", reason), (
            f"{key} names no converting lane"
        )
    for key, reason in EXEMPT.items():
        assert reason.strip(), f"{key} has no reason"


# ----------------------------------------------------------------------------- selftest


@pytest.mark.parametrize(
    ("rel", "text", "detector"),
    [
        ("a.sh", 'docker ps --filter "name=backend"\n', D1),
        ("a.sh", "docker ps --filter name=${SVC} -q\n", D1),
        ("a.py", 'argv = ["docker", "ps", "--filter", f"name={svc}"]\n', D1),
        ("a.sh", "docker exec opentranscribe-postgres psql\n", D2),
        ("a.sh", "docker logs -f transcribe-app-celery-worker\n", D2),
        ("a.py", 'run("docker exec opentranscribe-backend ls")\n', D2),
        ("a.sh", 'API_BASE="http://localhost:5174/api"\n', D3),
        ("a.sh", 'PORT="${BACKEND_PORT:-5174}"\n', D3),
        ("a.py", "import os\nP = os.environ.get('BACKEND_PORT', '5174')\n", D3),
        ("a.py", 'U = "http://127.0.0.1:5176/x"\n', D3),
        ("a.sh", "if port_open 5176; then echo up; fi\n", D3),
        ("a.sh", "docker compose -f docker-compose.x.yml up -d\n", D4),
        ("a.py", 'run(["docker", "compose", "-f", "x.yml", "up"])\n', D4),
        ("a.sh", "docker compose \\\n  -f x.yml up\n", D4),
    ],
)
def test_detector_fires(rel: str, text: str, detector: str) -> None:
    assert detector in findings_for(rel, text)


@pytest.mark.parametrize(
    ("rel", "text", "detector"),
    [
        ("a.sh", 'docker ps --filter "name=^ot-testdb-${hex}$"\n', D1),
        ("a.sh", 'docker ps --filter "label=com.docker.compose.project=$P"\n', D1),
        ("a.sh", '# docker ps --filter "name=backend"\n', D1),
        ("a.py", 'argv = ["docker", "ps", "--filter", f"name=^{svc}$"]\n', D1),
        ("a.sh", 'docker exec "$PG" psql\n', D2),
        ("a.sh", 'docker exec "$(st_container postgres)" psql\n', D2),
        ("a.sh", "cat <<'EOF'\ndocker exec opentranscribe-postgres psql\nEOF\n", D2),
        ("a.sh", 'source stack-target.sh\nAPI="http://localhost:5174/api"\n', D3),
        ("a.py", "import stack_target\nP = 'http://localhost:5174'\n", D3),
        ("a.sh", 'U="http://localhost:${BACKEND_PORT}/api"\n', D3),
        ("a.py", "import os\nP = os.environ.get('SOME_PORT', '8000')\n", D3),
        ("a.sh", 'docker compose -p "$ST_PROJECT" -f x.yml up\n', D4),
        ("a.sh", "COMPOSE_PROJECT_NAME=p docker compose -f x.yml up\n", D4),
        ("a.py", 'run(["docker", "compose", "-p", p, "-f", "x.yml", "up"])\n', D4),
        ("a.sh", "docker compose ps\n", D4),
    ],
)
def test_detector_stays_clean(rel: str, text: str, detector: str) -> None:
    assert detector not in findings_for(rel, text)


def test_the_known_wrong_stack_sites_are_detected() -> None:
    """The red proof: these five are the incidents behind the guard. If the detectors stop
    seeing them the guard is passing on nothing, whatever PENDING says."""
    hits = scan()
    for key in [
        ("scripts/lite-smoke.sh", D1),
        ("scripts/gpu-scale-smoke.sh", D1),
        ("scripts/run-mutation-tests.sh", D3),
        ("backend/tests/fixtures/search_corpus_stack.py", D3),
        ("backend/tests/e2e/idp_overlay.py", D4),
    ]:
        assert key in hits or key not in {**PENDING, **EXEMPT}, f"{key} was fixed: drop it"
        assert key in hits, f"detector no longer sees {key}"


def test_a_pending_key_for_a_clean_site_is_reported_stale() -> None:
    """Must-fire and must-stay-clean for the shrink-only rule itself."""
    live = ("scripts/a.sh", D1)
    fixed = ("scripts/b.sh", D1)
    assert stale_entries({live: True}, [live, fixed]) == [fixed]
    assert stale_entries({live: True, fixed: True}, [live, fixed]) == []


def test_every_exemption_names_a_reason_of_substance() -> None:
    assert EXEMPT, "the exemption table is empty: this test would pass on nothing"
    for key, reason in EXEMPT.items():
        assert len(reason.split()) >= 6, f"{key} needs a written reason"
