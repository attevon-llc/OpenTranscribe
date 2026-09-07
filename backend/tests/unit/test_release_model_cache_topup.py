"""A shared model cache that is missing a subdirectory must be REPAIRED, and say so.

WHAT WENT WRONG

`mc_seed_subdir` opened with a bare ``[[ -d "$src" ]] || return 0``. A caller asking for a
subdirectory the source did not have got silence and a `gr_ok "model cache seeded"` line.

MEASURED 2026-09-07, on this host's real shared rehearsal cache
(`/mnt/nvm/opentranscribe-test-runs/.shared-model-cache`): `.seeded-from-live` dated
2026-08-10, **no `diar-native` directory at all**, while the live cache held 462 MB of
exported ONNX/PLDA weights. `test-fresh-install.sh` and `test-lite-mode.sh` both list
`diar-native` in their `mc_seed_cache` call, so both silently started with nothing and paid a
full HuggingFace-backed ONNX export at first backend boot — over the network, mid-rehearsal,
on the exact path the pre-seeding exists to remove.

Two causes, and fixing either alone leaves the other:

* the silence (`mc_seed_subdir` now warns);
* the repair was PRIVATE to `test-upgrade.sh`, hand-rolled for `diar-native` alone, inside its
  own reuse branch — so the two scenarios that consume the shared cache could never fix it,
  and the next subdir added would have repeated the story verbatim. It is now
  `mc_topup_from_live`, generalised over a subdir list and called from all three phase-03s.

These tests run the real `lib/model-cache.sh` against real trees on disk. A grep would pass
against a helper that does nothing — which is precisely the state being fixed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_DIR = REPO_ROOT / "scripts" / "release-tests" / "lib"
MODEL_CACHE_LIB = LIB_DIR / "model-cache.sh"
SCENARIOS = {
    "test-upgrade.sh": REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh",
    "test-fresh-install.sh": REPO_ROOT / "scripts" / "release-tests" / "test-fresh-install.sh",
    "test-lite-mode.sh": REPO_ROOT / "scripts" / "release-tests" / "test-lite-mode.sh",
}

pytestmark = pytest.mark.skipif(
    not MODEL_CACHE_LIB.exists(), reason="release-test model-cache lib not in this checkout"
)

_GR_STUBS = (
    'gr_log(){ echo "LOG: $*" >&2; }; gr_ok(){ echo "OK: $*" >&2; }; '
    'gr_warn(){ echo "WARN: $*" >&2; }; '
    'gr_die(){ echo "DIE: $*" >&2; exit 1; }; '
)


def _run_lib(snippet: str) -> subprocess.CompletedProcess[str]:
    script = f"set -uo pipefail\n{_GR_STUBS}\nsource {MODEL_CACHE_LIB}\n{snippet}\n"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=120, check=False
    )


def _populate(root: Path, sub: str, *, files: int = 2) -> Path:
    d = root / sub
    d.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (d / f"weight{i}.onnx").write_text(f"payload {sub} {i}\n", encoding="utf-8")
    return d


@pytest.mark.unit
def test_a_missing_source_subdir_is_warned_about_not_swallowed(tmp_path: Path) -> None:
    """The silence is the bug: every caller reported a successful seed."""
    live = tmp_path / "live"
    shared = tmp_path / "shared"
    _populate(live, "torch")
    shared.mkdir()

    proc = _run_lib(f'mc_seed_subdir "{live}" "{shared}" "diar-native"')

    assert proc.returncode == 0, proc.stderr
    assert "WARN:" in proc.stderr, (
        f"a subdir the source does not have must be announced — it changes the run's "
        f"duration and its network dependence:\n{proc.stderr}"
    )
    assert "diar-native" in proc.stderr, proc.stderr


@pytest.mark.unit
def test_topup_fills_an_absent_subdir(tmp_path: Path) -> None:
    live = tmp_path / "live"
    shared = tmp_path / "shared"
    _populate(live, "diar-native")
    _populate(shared, "torch")

    proc = _run_lib(f'mc_topup_from_live "{live}" "{shared}" torch diar-native')

    assert proc.returncode == 0, proc.stderr
    copied = sorted(p.name for p in (shared / "diar-native").iterdir())
    assert copied == ["weight0.onnx", "weight1.onnx"], f"{copied}\n{proc.stderr}"


@pytest.mark.unit
def test_topup_treats_an_empty_subdir_as_missing(tmp_path: Path) -> None:
    """This is the state that actually occurred, and a `-d` test reports it as present.

    Every caller runs `mkdir -p` over the whole subdir list before seeding, so the shell of a
    never-populated directory always exists. A repair keyed on `[[ -d ]]` would have declared
    the real shared cache healthy on every run for four weeks.
    """
    live = tmp_path / "live"
    shared = tmp_path / "shared"
    _populate(live, "diar-native")
    (shared / "diar-native").mkdir(parents=True)

    proc = _run_lib(f'mc_topup_from_live "{live}" "{shared}" diar-native')

    assert proc.returncode == 0, proc.stderr
    assert (shared / "diar-native" / "weight0.onnx").is_file(), (
        f"an empty directory is a missing subdir, not a seeded one:\n{proc.stderr}"
    )


@pytest.mark.unit
def test_topup_leaves_an_already_populated_subdir_alone(tmp_path: Path) -> None:
    """Must-stay-clean: a top-up that re-copies everything is a multi-GB no-op every run."""
    live = tmp_path / "live"
    shared = tmp_path / "shared"
    _populate(live, "huggingface")
    _populate(shared, "huggingface")
    existing = shared / "huggingface" / "weight0.onnx"
    existing.write_text("do not overwrite me\n", encoding="utf-8")

    proc = _run_lib(f'mc_topup_from_live "{live}" "{shared}" huggingface')

    assert proc.returncode == 0, proc.stderr
    assert existing.read_text(encoding="utf-8") == "do not overwrite me\n", proc.stderr
    assert "topped up" not in proc.stderr, proc.stderr


@pytest.mark.unit
def test_topup_never_writes_to_the_live_cache(tmp_path: Path) -> None:
    """The live cache is this host's WORKING model directory. It is a read-only source."""
    live = tmp_path / "live"
    shared = tmp_path / "shared"
    _populate(live, "diar-native")
    shared.mkdir()
    before = {p.name: p.read_text(encoding="utf-8") for p in (live / "diar-native").iterdir()}

    _run_lib(f'mc_topup_from_live "{live}" "{shared}" diar-native pyannote')

    after = {p.name: p.read_text(encoding="utf-8") for p in (live / "diar-native").iterdir()}
    assert after == before, "the live cache must not be modified by a top-up"


@pytest.mark.unit
def test_topped_up_diar_native_is_a_real_copy_not_a_hardlink(tmp_path: Path) -> None:
    """MC_NO_HARDLINK_SUBDIRS must still hold on the top-up path (issue #670).

    `diar-native`'s files are rewritten IN PLACE by `diar-server provision-models`, and
    test-upgrade.sh deliberately runs an OLDER release's binary against them — a hardlink
    would let that rewrite reach back into this host's live diarizer weights. The repair path
    is a new way into the same copy, so it needs the same guarantee.
    """
    live = tmp_path / "live"
    shared = tmp_path / "shared"
    _populate(live, "diar-native")
    shared.mkdir()

    proc = _run_lib(f'mc_topup_from_live "{live}" "{shared}" diar-native')

    assert proc.returncode == 0, proc.stderr
    links = [p.stat().st_nlink for p in (shared / "diar-native").iterdir()]
    assert links and all(n == 1 for n in links), (
        f"topped-up diar-native files are multiply linked to the LIVE cache: {links}"
    )


@pytest.mark.unit
def test_a_live_cache_that_does_not_exist_warns_and_does_not_abort(tmp_path: Path) -> None:
    """A checkout with no models/ directory must still be able to run a rehearsal."""
    shared = tmp_path / "shared"
    shared.mkdir()

    proc = _run_lib(f'mc_topup_from_live "{tmp_path}/nope" "{shared}" diar-native')

    assert proc.returncode == 0, proc.stderr
    assert "WARN:" in proc.stderr, proc.stderr


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_repairs_the_shared_cache(name: str) -> None:
    """All three phase-03s, so whichever runs FIRST repairs it for the other two.

    Only `test-upgrade.sh` could before, and it is the one scenario that does NOT copy the
    shared cache per-run — so the two that do consumed a cache nothing they ran could fix.
    """
    path = SCENARIOS[name]
    if not path.exists():
        pytest.skip(f"{name} not present in this checkout")
    text = path.read_text(encoding="utf-8")

    assert "mc_topup_from_live " in text, (
        f"{name} seeds from the shared model cache but can never repair it"
    )
    assert "diar-native" in text, f"{name} must top up diar-native — see issue #670"
