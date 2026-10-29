"""A visual-baseline capture must photograph EXACTLY the seed corpus (#973, #1238).

The gate is ``assert_corpus_is_the_seed`` in ``scripts/e2e/update-visual-baselines.sh``. Its
history is two defects of one shape -- the image measures the deployment, not the UI:

* #973: it exited 0 on ``files and not busy``, so a corpus in which EVERY file failed passed as
  "0 completed file(s)" and the capture photographed an empty library.
* #1238: it never compared the library to anything, and it read only page 1 of ``/api/files``
  (default 20). The seeder re-uploaded on every start, so a reused capture stack held 12 files,
  the gate said "ready", and the gallery baseline taken at 9 failed a compare at 12 (6.8%
  against a 0.5% tolerance). A card count cannot be masked, so only the corpus can be asserted.

Tested against the real script text: the Python block embedded in the shell function is
extracted verbatim and run against a stub backend (``_visual_corpus_stub``) that paginates and
sanitizes filenames like the real one. Each refusal test names a library the OLD gate accepted.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit._visual_corpus_stub import StubBackend

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINES_SCRIPT = REPO_ROOT / "scripts" / "e2e" / "update-visual-baselines.sh"
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed-fresh-deployment.sh"

#: Names chosen so the backend's sanitizer rewrites two of them (see the stub's docstring).
SEED = [
    ("seed_t1_899s_40d403ee_DOGE_s_Findings.mp3", 14721443),
    ("seed_t1_531s_a536922c_Will_the_Gaza_Ceasefire_Hold_.mp4", 18228379),
    ("seed_t1_520s_fa049b47_The_AI_Arms_Race_with_China.mp4", 24469909),
]


def _gate_source() -> str:
    """The Python block ``assert_corpus_is_the_seed`` feeds to the venv interpreter, verbatim."""
    text = BASELINES_SCRIPT.read_text(encoding="utf-8")
    fn = text.index("assert_corpus_is_the_seed() {")
    start = text.index("<<'PYEOF'", fn)
    body_start = text.index("\n", start) + 1
    body_end = text.index("\nPYEOF", body_start)
    return text[body_start:body_end]


def manifest_for(entries: list[tuple[str, int]], source: str = "/host/test_audio") -> str:
    return "\n".join([f"# source: {source}", *(f"{n}\t{s}" for n, s in entries)])


def run_gate(
    stub: StubBackend, manifest: str, *, timeout_s: int = 4
) -> subprocess.CompletedProcess[str]:
    """Run the real gate against ``stub`` for a seed ``manifest``."""
    env = {
        **os.environ,
        "OT_BACKEND_URL": stub.url,
        "OT_SEED_MANIFEST": manifest,
        "OT_FRESH_NAME": "visual",
        "OT_VISUAL_SEED_TIMEOUT": str(timeout_s),
        "OT_VISUAL_POLL_SECONDS": "0.2",
    }
    return subprocess.run(
        [sys.executable, "-I", "-c", _gate_source()],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def seeded_stub(*, copies: int = 1, status: str = "completed") -> StubBackend:
    stub = StubBackend()
    for _ in range(copies):
        for name, size in SEED:
            stub.add_file(name, size=size, status=status)
    return stub


class TestTheLibraryMustBeExactlyTheSeed:
    def test_the_exact_corpus_passes(self):
        with seeded_stub() as stub:
            result = run_gate(stub, manifest_for(SEED))

        assert result.returncode == 0, result.stderr
        assert "3 completed" in result.stdout
        assert "exactly the 3-file seed corpus" in result.stdout

    def test_four_starts_worth_of_files_is_refused(self):
        """THE issue: 12 files (4 x 3). The old gate passed this."""
        with seeded_stub(copies=4) as stub:
            result = run_gate(stub, manifest_for(SEED))

        assert result.returncode == 1, result.stdout
        assert "expected 3 file(s), found 12" in result.stderr
        assert "uploaded more than once" in result.stderr
        assert "Will_the_Gaza_Ceasefire_Hold_.mp4 x4" in result.stderr
        assert "fresh-destroy visual --yes" in result.stderr

    def test_a_missing_seed_file_is_refused(self):
        with StubBackend() as stub:
            for name, size in SEED[:2]:
                stub.add_file(name, size=size)
            result = run_gate(stub, manifest_for(SEED))

        assert result.returncode == 1, result.stdout
        assert "missing: " + SEED[2][0] in result.stderr

    def test_a_file_outside_the_seed_is_refused_even_when_the_count_would_match(self):
        """Two seed files plus one leaked test row is 3 files: a count cannot tell."""
        with StubBackend() as stub:
            for name, size in SEED[:2]:
                stub.add_file(name, size=size)
            stub.add_file("e2e-owned-deadbeef01.wav", size=1)
            result = run_gate(stub, manifest_for(SEED))

        assert result.returncode == 1, result.stdout
        assert "not part of the seed: e2e-owned-deadbeef01.wav" in result.stderr
        assert "missing: " + SEED[2][0] in result.stderr
        assert "test-cleanup --fresh visual --dry-run" in result.stderr

    def test_a_replaced_file_with_the_same_name_is_refused_by_its_size(self):
        with StubBackend() as stub:
            for name, size in SEED[:2]:
                stub.add_file(name, size=size)
            stub.add_file(SEED[2][0], size=SEED[2][1] + 1)
            result = run_gate(stub, manifest_for(SEED))

        assert result.returncode == 1, result.stdout
        # A seed file is identified by extension + byte size, so a replacement with other bytes is
        # reported as the original missing and the replacement as not part of the seed.
        assert "missing: " + SEED[2][0] in result.stderr
        assert "not part of the seed: " in result.stderr

    def test_the_status_match_is_case_insensitive(self):
        with seeded_stub(status="Completed") as stub:
            result = run_gate(stub, manifest_for(SEED))

        assert result.returncode == 0, result.stderr


class TestEverySeedFileMustHaveCompleted:
    def test_a_corpus_where_every_file_failed_is_not_ready(self):
        """#973: 'nothing is still running' is not 'the corpus is ready'."""
        with seeded_stub(status="error") as stub:
            result = run_gate(stub, manifest_for(SEED))

        assert result.returncode == 1, result.stdout
        assert "none completed" in result.stderr.lower()
        assert "error" in result.stderr.lower()

    def test_one_failed_seed_file_among_completed_ones_is_refused(self):
        """The corpus is the set, so a partly failed one is not 'enough'."""
        with StubBackend() as stub:
            stub.add_file(SEED[0][0], size=SEED[0][1])
            stub.add_file(SEED[1][0], size=SEED[1][1])
            stub.add_file(SEED[2][0], size=SEED[2][1], status="error")
            result = run_gate(stub, manifest_for(SEED))

        assert result.returncode == 1, result.stdout
        assert "1 of 3 seed file(s) did not complete" in result.stderr
        assert f"{SEED[2][0]} (error)" in result.stderr
        assert "does not recover by waiting" in result.stderr

    def test_a_file_still_processing_is_waited_for_not_passed(self):
        with seeded_stub() as stub:
            stub.library[0]["status"] = "processing"
            result = run_gate(stub, manifest_for(SEED), timeout_s=2)

        assert result.returncode == 1, result.stdout
        assert "timed out waiting" in result.stderr
        assert "1 still processing" in result.stdout


class TestAnEmptyOrSyntheticManifestIsNotAVacuousPass:
    """`all([])` is True: an empty expectation would 'match' any library, even an empty one."""

    def test_an_empty_manifest_fails_against_an_empty_library(self):
        with StubBackend() as stub:
            result = run_gate(stub, "# source: /host/test_audio")

        assert result.returncode == 1, result.stdout
        assert "empty or synthetic" in result.stderr

    def test_an_empty_manifest_fails_against_a_populated_library(self):
        with seeded_stub() as stub:
            result = run_gate(stub, "# source: /host/test_audio")

        assert result.returncode == 1, result.stdout
        assert "empty or synthetic" in result.stderr

    def test_a_synthetic_manifest_is_refused(self):
        with seeded_stub() as stub:
            result = run_gate(stub, manifest_for(SEED, source="synthetic"))

        assert result.returncode == 1, result.stdout
        assert "empty or synthetic" in result.stderr

    def test_a_malformed_manifest_line_is_refused(self):
        with seeded_stub() as stub:
            result = run_gate(stub, "# source: /d\nname.mp4\tnot-a-size")

        assert result.returncode == 1, result.stdout
        assert "malformed seed manifest line" in result.stderr


class TestThePaginationWindowIsNotTheLibrary:
    """`/api/files` serves 20 per page by default and at most 100; page 1 is a window."""

    @staticmethod
    def _big_seed(n: int) -> list[tuple[str, int]]:
        return [(f"seed_{i:03d}_clip.mp4", 1000 + i) for i in range(n)]

    def test_a_corpus_spanning_two_pages_passes_when_exact(self):
        entries = self._big_seed(101)
        with StubBackend() as stub:
            for name, size in entries:
                stub.add_file(name, size=size)
            result = run_gate(stub, manifest_for(entries))

        assert result.returncode == 0, result.stderr
        assert "101 completed" in result.stdout

    def test_a_failed_file_on_page_two_is_seen(self):
        entries = self._big_seed(101)
        with StubBackend() as stub:
            for name, size in entries[:-1]:
                stub.add_file(name, size=size)
            stub.add_file(*entries[-1][:1], size=entries[-1][1], status="error")
            result = run_gate(stub, manifest_for(entries))

        assert result.returncode == 1, result.stdout
        assert "seed_100_clip.mp4 (error)" in result.stderr

    def test_a_file_still_processing_on_page_two_is_waited_for(self):
        entries = self._big_seed(101)
        with StubBackend() as stub:
            for name, size in entries[:-1]:
                stub.add_file(name, size=size)
            stub.add_file(entries[-1][0], size=entries[-1][1], status="processing")
            result = run_gate(stub, manifest_for(entries), timeout_s=2)

        assert result.returncode == 1, result.stdout
        assert "timed out waiting" in result.stderr


def test_an_unreadable_listing_is_retried_until_the_deadline_not_passed():
    with StubBackend(fail_listing=True) as stub:
        result = run_gate(stub, manifest_for(SEED), timeout_s=2)

    assert result.returncode == 1, result.stdout
    assert "timed out waiting" in result.stderr


class TestTheSeederAndTheGateAgree:
    """End to end over the real seeder: what it uploads is what the gate accepts."""

    @pytest.mark.skipif(
        subprocess.run(
            ["bash", "-c", "command -v curl"], capture_output=True, check=False
        ).returncode
        != 0,
        reason="curl is not installed; the seeder shells out to it for every upload",
    )
    def test_a_reused_stack_seeded_repeatedly_still_passes_the_gate(self, tmp_path: Path):
        media = tmp_path / "media"
        media.mkdir()
        for index, (name, _size) in enumerate(SEED):
            (media / name).write_bytes(b"x" * (100 + index * 10))

        env = {**os.environ, "SEED_MEDIA_DIR": str(media), "SEED_MAX_FILES": "3"}
        manifest = subprocess.run(
            ["bash", str(SEED_SCRIPT), "--manifest"],
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        with StubBackend() as stub:
            for _ in range(4):
                run = subprocess.run(
                    ["bash", str(SEED_SCRIPT)],
                    env={**env, "BACKEND_URL": stub.url, "SEED_WAIT_TIMEOUT": "20"},
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                assert run.returncode == 0, run.stderr
            result = run_gate(stub, manifest)

        assert result.returncode == 0, f"{result.stderr}\n{manifest}"
        assert "exactly the 3-file seed corpus" in result.stdout


def test_the_gate_block_is_still_extractable_from_the_wrapper():
    """The anchor the extraction uses; a rename must fail here, loudly, not as a KeyError."""
    source = _gate_source()

    assert "collections.Counter" in source
    assert "requests.get" in source
