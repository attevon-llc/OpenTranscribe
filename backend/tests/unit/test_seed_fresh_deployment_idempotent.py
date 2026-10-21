"""``seed-fresh-deployment.sh`` must be idempotent per file (#1238).

The seeder uploaded its media on EVERY ``--seed-benchmark`` start with no existence check, and
the visual-baseline capture stack keeps its volumes between runs. Measured 2026-10-12: four
starts left 12 files, and gallery baselines taken at 9 files failed a compare at 12 (6.8%
differs against a 0.5% tolerance). The committed ``gallery-light.png`` itself shows the
accumulation: two titles repeated four times each.

These tests drive the REAL script against a stub backend that behaves like the real one in the
two ways that matter (see ``_visual_corpus_stub``): ``/api/files`` is paginated at 100, and the
stored ``filename`` is SANITIZED while ``title`` keeps the original stem. The second point is
not decoration: 11 of the 40 files in the benchmark corpus are renamed by the sanitizer, so a
seeder that compared the on-disk basename with ``filename`` would re-upload exactly those files
on every start and look idempotent on the others.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit._visual_corpus_stub import StubBackend

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed-fresh-deployment.sh"

#: Three media names, two of which the backend's sanitizer rewrites (a trailing underscore
#: before the extension, and a run of underscores), mirroring the real benchmark corpus.
SEED_NAMES = [
    "seed_t1_899s_40d403ee_DOGE_s_Findings.mp3",
    "seed_t1_531s_a536922c_Will_the_Gaza_Ceasefire_Hold_.mp4",
    "seed_t1_305s_f90905ac_Watching___Obama___Trump.mp4",
]

pytestmark = pytest.mark.skipif(
    subprocess.run(["bash", "-c", "command -v curl"], capture_output=True, check=False).returncode
    != 0,
    reason="curl is not installed; the seeder shells out to it for every upload",
)


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    """A directory of fake media with distinct sizes (so 'the 3 smallest' is unambiguous)."""
    d = tmp_path / "media"
    d.mkdir()
    for index, name in enumerate(SEED_NAMES):
        (d / name).write_bytes(b"x" * (100 + index * 10))
    # A larger file the seeder must NOT pick (SEED_MAX_FILES=3 selects the smallest three).
    (d / "zz_too_big.mp4").write_bytes(b"x" * 5000)
    return d


def run_seed(
    media: Path, url: str | None, *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SEED_MEDIA_DIR": str(media),
        "SEED_WAIT_TIMEOUT": "20",
        "SEED_MAX_FILES": "3",
    }
    if url is not None:
        env["BACKEND_URL"] = url
    return subprocess.run(
        ["bash", str(SEED_SCRIPT), *args],
        env=env,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_first_run_against_an_empty_library_uploads_every_seed_file(media_dir: Path) -> None:
    with StubBackend() as stub:
        result = run_seed(media_dir, stub.url)

        assert result.returncode == 0, result.stderr
        assert sorted(stub.uploaded_names) == sorted(SEED_NAMES)
        assert "uploaded 3, already present 0, failed 0 of 3" in result.stdout


def test_a_second_run_against_the_same_stack_uploads_nothing(media_dir: Path) -> None:
    """The issue's reproduction: start twice, expect 3 files, not 6."""
    with StubBackend() as stub:
        first = run_seed(media_dir, stub.url)
        second = run_seed(media_dir, stub.url)

        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert len(stub.uploaded_names) == 3, (
            f"the second run uploaded again: {stub.uploaded_names}"
        )
        assert len(stub.library) == 3
        assert "uploaded 0, already present 3, failed 0 of 3" in second.stdout


def test_four_starts_leave_exactly_the_seed_corpus(media_dir: Path) -> None:
    """The measured failure: four starts left 12 files."""
    with StubBackend() as stub:
        for _ in range(4):
            assert run_seed(media_dir, stub.url).returncode == 0

        assert len(stub.library) == 3, (
            f"{len(stub.library)} files after four starts; expected the 3-file seed corpus"
        )


def test_a_partly_seeded_stack_gets_only_the_missing_files(media_dir: Path) -> None:
    """A half-failed first seed must complete on the next start, not be skipped wholesale."""
    with StubBackend() as stub:
        stub.add_file(SEED_NAMES[0], size=100)

        result = run_seed(media_dir, stub.url)

        assert result.returncode == 0, result.stderr
        assert sorted(stub.uploaded_names) == sorted(SEED_NAMES[1:])
        assert "uploaded 2, already present 1, failed 0 of 3" in result.stdout


def test_unrelated_files_in_the_library_do_not_count_as_the_seed(media_dir: Path) -> None:
    """Skip-if-anything-exists would never seed a stack that holds somebody's own file."""
    with StubBackend() as stub:
        stub.add_file("somebody_elses_meeting.mp4", size=10)

        result = run_seed(media_dir, stub.url)

        assert result.returncode == 0, result.stderr
        assert sorted(stub.uploaded_names) == sorted(SEED_NAMES)


def test_seed_files_beyond_the_first_page_are_still_recognised(media_dir: Path) -> None:
    """The listing is paginated (100 per page); reading page 1 only is a window, not the library."""
    with StubBackend() as stub:
        for index in range(120):
            stub.add_file(f"filler_{index:03d}.mp4", size=1)
        for name in SEED_NAMES:
            stub.add_file(name, size=100)  # lands on page 2

        result = run_seed(media_dir, stub.url)

        assert result.returncode == 0, result.stderr
        assert stub.uploaded_names == [], (
            f"re-uploaded files already present: {stub.uploaded_names}"
        )
        assert "already present 3" in result.stdout
        assert ("GET", "/api/files") in stub.requests


def test_an_unreadable_listing_uploads_nothing(media_dir: Path) -> None:
    """Fail closed. 'Upload anyway' on an unknowable state is the original bug."""
    with StubBackend(fail_listing=True) as stub:
        result = run_seed(media_dir, stub.url)

        assert result.returncode == 0, "the seeder is non-fatal to its caller by contract"
        assert stub.uploaded_names == []
        assert "uploading NOTHING" in result.stdout


def test_a_failed_upload_is_counted_and_the_next_run_completes_it(media_dir: Path) -> None:
    """curl failures are non-fatal; per-file idempotence is what makes the retry converge."""
    with StubBackend(reject_uploads=True) as stub:
        failed = run_seed(media_dir, stub.url)

        assert failed.returncode == 0, failed.stderr
        assert "uploaded 0, already present 0, failed 3 of 3" in failed.stdout
        assert stub.library == []

        stub.reject_uploads = False
        retried = run_seed(media_dir, stub.url)

        assert retried.returncode == 0, retried.stderr
        assert len(stub.library) == 3
        assert "uploaded 3, already present 0, failed 0 of 3" in retried.stdout


class TestManifest:
    def test_it_lists_the_files_it_would_seed_with_their_sizes_and_no_network(
        self, media_dir: Path
    ) -> None:
        # No BACKEND_URL at all: a manifest that touched the network would fail or hang here.
        result = run_seed(media_dir, None, "--manifest")

        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert lines[0] == f"# source: {media_dir}"
        listed = dict(line.split("\t") for line in lines[1:])
        assert listed == {name: str(100 + i * 10) for i, name in enumerate(SEED_NAMES)}

    def test_it_matches_what_a_real_run_uploads(self, media_dir: Path) -> None:
        """One selection function: the expectation cannot drift from the corpus."""
        manifest = run_seed(media_dir, None, "--manifest")
        with StubBackend() as stub:
            run_seed(media_dir, stub.url)

            names = [line.split("\t")[0] for line in manifest.stdout.splitlines()[1:]]
            assert sorted(names) == sorted(stub.uploaded_names)
            sizes = {
                line.split("\t")[0]: int(line.split("\t")[1])
                for line in manifest.stdout.splitlines()[1:]
            }
            stored = {f["title"] + Path(f["filename"]).suffix: f["file_size"] for f in stub.library}
            assert sizes == stored

    def test_with_no_media_it_says_synthetic_rather_than_printing_an_empty_success(
        self, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        result = run_seed(empty, None, "--manifest", cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[0] == "# source: synthetic"

    def test_an_unknown_argument_is_refused(self, media_dir: Path) -> None:
        result = run_seed(media_dir, None, "--nope")

        assert result.returncode == 2
        assert "unknown argument" in result.stderr
