"""A visual-baseline capture must never run against a corpus with nothing transcribed (#973).

Run from a git worktree, a capture seeded **zero completed files** and photographed the result
anyway. The failures it produced (``no such index [transcript_chunks]`` on the file-detail
surface, empty galleries) are indistinguishable from real UI regressions on the branch under
test, which is what made it expensive. Two independent defects combined:

1. ``scripts/seed-fresh-deployment.sh`` defaulted ``SEED_MEDIA_DIR`` to the RELATIVE path
   ``benchmark/test_audio``. ``benchmark/`` is gitignored, so it exists only in the main
   checkout; from ``.claude/worktrees/<name>`` the path resolves to nothing, the script falls
   back to synthetic silence, and the pipeline (correctly) marks every file ``error``.
2. ``wait_for_seeded_files`` in ``scripts/e2e/update-visual-baselines.sh`` exits 0 when
   ``files and not busy``. ``error`` is neither ``processing`` nor ``pending``, so a corpus in
   which EVERY file failed satisfied it and reported "0 completed file(s)" as success.

Both are tested against the real script text, not a re-implementation: the gate by running
the Python block embedded in the shell function against a stub backend, the path by sourcing
only the resolver function inside a genuine ``git worktree``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINES_SCRIPT = REPO_ROOT / "scripts" / "e2e" / "update-visual-baselines.sh"
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed-fresh-deployment.sh"


def _gate_source() -> str:
    """The Python block ``wait_for_seeded_files`` feeds to the venv interpreter, verbatim."""
    text = BASELINES_SCRIPT.read_text(encoding="utf-8")
    fn = text.index("wait_for_seeded_files() {")
    start = text.index("<<'PYEOF'", fn)
    body_start = text.index("\n", start) + 1
    body_end = text.index("\nPYEOF", body_start)
    return text[body_start:body_end]


def _run_gate(files: list[dict], *, timeout_s: int = 60) -> subprocess.CompletedProcess[str]:
    """Run the real gate against a stub backend that serves ``files``."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
            self._send({"access_token": "stub-token"})

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            self._send(files)

        def log_message(self, *_args: object) -> None:  # keep test output clean
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            **os.environ,
            "OT_BACKEND_URL": f"http://127.0.0.1:{server.server_address[1]}",
            "OT_VISUAL_SEED_TIMEOUT": "20",
        }
        return subprocess.run(
            [sys.executable, "-I", "-c", _gate_source()],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()


class TestTheProcessingGateRequiresACompletedFile:
    def test_a_corpus_where_every_file_failed_is_not_ready(self):
        result = _run_gate([{"status": "error"}, {"status": "error"}, {"status": "error"}])

        assert result.returncode != 0, (
            "the gate passed a corpus with ZERO completed files — the capture would then "
            f"photograph an empty library.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # It must say WHY, in a way a person scanning a multi-minute start-up log can act on.
        assert "none completed" in result.stderr.lower()
        assert "error" in result.stderr.lower()

    def test_one_completed_file_among_failures_is_enough(self):
        """The control: the gate is not simply refusing everything."""
        result = _run_gate([{"status": "error"}, {"status": "completed"}])

        assert result.returncode == 0, result.stderr
        assert "1 completed" in result.stdout

    def test_an_all_completed_corpus_passes(self):
        result = _run_gate([{"status": "completed"}, {"status": "completed"}])

        assert result.returncode == 0, result.stderr
        assert "2 completed" in result.stdout

    def test_the_status_match_is_case_insensitive(self):
        result = _run_gate([{"status": "Completed"}])

        assert result.returncode == 0, result.stderr


def _git(cwd: Path, *args: str) -> str:
    """Run git in a sandbox that ignores the developer's own global/system config."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _resolver_function() -> str:
    """The seed script's default-directory resolver, extracted verbatim."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^_default_seed_media_dir\(\) \{\n.*?^\}\n", text, re.DOTALL | re.MULTILINE)
    assert match is not None, "seed-fresh-deployment.sh has no _default_seed_media_dir()"
    return match.group(0)


def _resolve_from(cwd: Path) -> str:
    return subprocess.run(
        ["bash", "-c", f"set -u\n{_resolver_function()}\n_default_seed_media_dir"],
        cwd=cwd,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo_with_corpus(tmp_path: Path) -> Path:
    """A main checkout holding a gitignored ``benchmark/test_audio`` corpus."""
    main = tmp_path / "main-checkout"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README").write_text("x\n", encoding="utf-8")
    (main / ".gitignore").write_text("benchmark/\n", encoding="utf-8")
    _git(main, "add", "README", ".gitignore")
    _git(main, "commit", "-q", "-m", "init")
    corpus = main / "benchmark" / "test_audio"
    corpus.mkdir(parents=True)
    (corpus / "real.wav").write_bytes(b"RIFF")
    return main


class TestTheSeedDirectoryResolvesFromAWorktree:
    def test_a_worktree_finds_the_main_checkouts_gitignored_corpus(
        self, repo_with_corpus: Path, tmp_path: Path
    ):
        worktree = tmp_path / "wt"
        _git(repo_with_corpus, "worktree", "add", "-q", "--detach", str(worktree))
        assert not (worktree / "benchmark").exists(), "precondition: gitignored, so absent here"

        resolved = _resolve_from(worktree)

        assert resolved == str(repo_with_corpus / "benchmark" / "test_audio")
        assert Path(resolved).is_dir()

    def test_the_main_checkout_resolves_to_its_own_corpus_as_an_absolute_path(
        self, repo_with_corpus: Path
    ):
        resolved = _resolve_from(repo_with_corpus)

        assert resolved == str(repo_with_corpus / "benchmark" / "test_audio")

    def test_with_no_corpus_anywhere_the_relative_default_is_kept(self, tmp_path: Path):
        bare = tmp_path / "bare"
        bare.mkdir()
        _git(bare, "init", "-q")

        assert _resolve_from(bare) == "benchmark/test_audio"

    def test_outside_a_git_repository_the_relative_default_is_kept(self, tmp_path: Path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()

        assert _resolve_from(plain) == "benchmark/test_audio"

    def test_an_explicit_seed_media_dir_still_wins(self):
        text = SEED_SCRIPT.read_text(encoding="utf-8")

        assert 'SEED_MEDIA_DIR="${SEED_MEDIA_DIR:-$(_default_seed_media_dir)}"' in text
