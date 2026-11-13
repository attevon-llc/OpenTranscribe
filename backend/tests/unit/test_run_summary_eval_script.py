"""``scripts/run-summary-eval.sh``: argument refusals, GPU 0 refusal, and a faithful dry-run.

The script starts a stack and a GPU-backed model, so none of that can run here. What can be
pinned is everything it decides BEFORE touching either: it refuses unsafe or incomplete
invocations, and ``--dry-run`` prints every command and executes none of them.
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-summary-eval.sh"
ARMS = ["--arm", "old=@module", "--arm", "new=/arms/new.json"]


def _closed_port() -> int:
    """A port nothing listens on, so a model server running on this host cannot change the plan."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "SUMMARY_EVAL_WORK_DIR": str(tmp_path / "work"),
        "LLM_TEST_PORT": str(_closed_port()),
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )


def test_both_arms_are_required(tmp_path: Path) -> None:
    result = _run(tmp_path, "--fresh", "x", "--arm", "old=@module", "--dry-run")
    assert result.returncode == 2
    assert "both required" in result.stderr


def test_a_run_needs_an_isolated_stack_or_an_explicit_server(tmp_path: Path) -> None:
    result = _run(tmp_path, *ARMS, "--dry-run")
    assert result.returncode == 2
    assert "--fresh NAME" in result.stderr


def test_an_unknown_option_is_a_misuse_error(tmp_path: Path) -> None:
    assert _run(tmp_path, "--bogus").returncode == 2


def test_a_non_positive_concurrency_is_a_misuse_error(tmp_path: Path) -> None:
    result = _run(tmp_path, "--fresh", "x", *ARMS, "--concurrency", "0", "--dry-run")
    assert result.returncode == 2
    assert "--concurrency" in result.stderr


def test_gpu_zero_is_refused_even_for_a_dry_run(tmp_path: Path) -> None:
    result = _run(tmp_path, "--fresh", "x", *ARMS, "--dry-run", LLM_TEST_GPU_DEVICE_ID="0")
    assert result.returncode == 2
    assert "GPU 0" in result.stderr
    assert "start dev" not in result.stdout  # nothing past the GPU check was even planned


def test_dry_run_prints_the_whole_sequence_and_creates_nothing(tmp_path: Path) -> None:
    result = _run(tmp_path, "--fresh", "sumeval", *ARMS, "--dry-run")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    steps = [ln for ln in out.splitlines() if ln.startswith("==> step")]
    assert [s.split(":")[0] for s in steps] == [f"==> step {n}" for n in range(1, 6)]
    for fragment in (
        "start dev --fresh sumeval",
        "--with-llm-test",
        "plan-slices",
        "benchmark_summary.py run --inputs smoke",
        "benchmark_summary.py project",
        "benchmark_summary.py run --inputs gate --runs 5",
        "benchmark_summary.py replay-merge",
        "benchmark_summary.py judge",
        "benchmark_summary.py report",
    ):
        assert fragment in out, fragment
    assert not (tmp_path / "work").exists()  # a dry-run writes no run directory


def test_an_external_server_skips_gpu_and_stack_management(tmp_path: Path) -> None:
    result = _run(tmp_path, "--llm-base-url", "http://localhost:1/v1", *ARMS, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "GPU check skipped" in result.stdout
    assert "start dev" not in result.stdout
    assert "--fresh" not in result.stdout


@pytest.mark.parametrize(("inputs", "present"), [("smoke", False), ("gate", True)])
def test_smoke_inputs_run_no_controls_replay_or_judge(
    tmp_path: Path, inputs: str, present: bool
) -> None:
    result = _run(
        tmp_path, "--llm-base-url", "http://localhost:1/v1", *ARMS, "--inputs", inputs, "--dry-run"
    )
    assert result.returncode == 0, result.stderr
    assert ("benchmark_summary.py replay-merge" in result.stdout) is present
    assert ("benchmark_summary.py judge" in result.stdout) is present


def test_the_remote_leg_is_opt_in_and_runs_the_anthropic_dialect_on_the_smoke_input(
    tmp_path: Path,
) -> None:
    plain = _run(tmp_path, "--llm-base-url", "http://localhost:1/v1", *ARMS, "--dry-run")
    assert "--provider anthropic" not in plain.stdout
    remote = _run(
        tmp_path, "--llm-base-url", "http://localhost:1/v1", *ARMS, "--remote-leg", "--dry-run"
    )
    assert remote.returncode == 0, remote.stderr
    line = next(ln for ln in remote.stdout.splitlines() if "--provider anthropic" in ln)
    assert "--inputs smoke" in line and "--model claude-haiku-4-5" in line
    assert "ANTHROPIC_API_KEY" not in remote.stdout  # the key is never echoed


def test_a_resumed_run_does_not_repeat_its_smoke(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "smoke").mkdir(parents=True)
    (run_dir / "smoke" / "records.jsonl").write_text('{"cell": "I1|old|0"}\n')
    result = _run(tmp_path, "--fresh", "sumeval", *ARMS, "--dry-run", "--resume-dir", str(run_dir))
    assert result.returncode == 0, result.stderr
    assert "not repeating it" in result.stdout
    assert "run --inputs smoke" not in result.stdout
    assert "run --inputs gate --runs 5" in result.stdout


def _serve_models() -> tuple[http.server.HTTPServer, int]:
    """A throwaway server answering GET /v1/models, standing in for a running vLLM."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def _fake_docker(tmp_path: Path, project: str, port: int) -> str:
    """A `docker` whose only container is in `project` and publishes `port`; returns PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    row = f"x-llm\\t{project}\\tllm-test-vllm\\trunning\\t/w\\t127.0.0.1:{port}->8000/tcp"
    (bindir / "docker").write_text(
        f"#!/bin/bash\n[[ \"$1\" == ps ]] && printf '{row}\\n'\nexit 0\n"
    )
    (bindir / "docker").chmod(0o755)
    return f"{bindir}:{os.environ['PATH']}"


def test_a_vllm_owned_by_another_project_is_never_reused(tmp_path: Path) -> None:
    server, port = _serve_models()
    try:
        path = _fake_docker(tmp_path, "transcribe-app", port)
        result = _run(
            tmp_path, "--fresh", "sumeval", *ARMS, "--dry-run", PATH=path, LLM_TEST_PORT=str(port)
        )
    finally:
        server.shutdown()
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "owned by 'transcribe-app'" in result.stderr


def test_a_vllm_owned_by_the_target_fresh_stack_is_reused(tmp_path: Path) -> None:
    server, port = _serve_models()
    try:
        path = _fake_docker(tmp_path, "otfresh-sumeval", port)
        result = _run(
            tmp_path, "--fresh", "sumeval", *ARMS, "--dry-run", PATH=path, LLM_TEST_PORT=str(port)
        )
    finally:
        server.shutdown()
    assert "belongs to otfresh-sumeval" in result.stdout, (result.stdout, result.stderr)
