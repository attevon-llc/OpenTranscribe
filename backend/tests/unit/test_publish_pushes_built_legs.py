"""`publish` must push EXACTLY the images `build` produced -- no rebuild, no re-scan.

THE BUG (release pipeline audit, P1-3)

`80-publish.sh` ran `docker-build-push.sh all` in push mode: a from-scratch rebuild of every
leg on a different BuildKit builder, plus a re-scan. Everything `scan` and `rehearse` had
validated was therefore a different artifact from what reached Docker Hub; only `:latest`
was provably equal to `:vX.Y.Z`.

APPROACH

Two real blocks of shell are extracted by marker and driven against a fake `docker` and a
fake registry (a directory of files), so the control flow, the ordering and every refusal are
the shipped code rather than a re-implementation:

* `# --- BEGIN leg-image-ids ---` in `40-build.sh` writes `legs.tsv` (image ID + architecture
  per leg).
* `# --- BEGIN push-built-legs ---` in `80-publish.sh` verifies local IDs against it BEFORE any
  push, pushes each leg, assembles the indexes from the pushed digests and reads the registry
  back.

Each refusal is shown red by a scenario that breaks exactly that property. What this cannot
show is a real `docker push` of an arm64 image loaded from the remote builder; see
`scripts/CLAUDE.md`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_SH = REPO_ROOT / "scripts" / "release" / "40-build.sh"
PUBLISH_SH = REPO_ROOT / "scripts" / "release" / "80-publish.sh"
CHECKER = REPO_ROOT / "scripts" / "lib" / "manifest_platform_check.py"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not (BUILD_SH.exists() and PUBLISH_SH.exists() and CHECKER.exists()),
        reason="release scripts not present in this checkout",
    ),
]

VERSION = "v9.9.9"
FAKE_PLATFORMS = (
    "backend\tcuda\tlinux/amd64\n"
    "blackwell\tblackwell\tlinux/arm64\n"
    "frontend\tmultiarch\tlinux/amd64,linux/arm64\n"
    "lite\tcpu\tlinux/amd64,linux/arm64\n"
)
REPOS = {"backend": "test/ot-backend", "frontend": "test/ot-frontend", "lite": "test/ot-lite"}
CAP = {"backend": "cuda", "frontend": "multiarch", "lite": "cpu"}
LEGS = [
    ("backend", "linux/amd64"),
    ("frontend", "linux/amd64"),
    ("frontend", "linux/arm64"),
    ("lite", "linux/amd64"),
    ("lite", "linux/arm64"),
]


def _tag(component: str, platform: str) -> str:
    return f"{REPOS[component]}:{VERSION}-{CAP[component]}-{platform.split('/')[1]}"


def _san(ref: str) -> str:
    return ref.replace("/", "_").replace(":", "_").replace("@", "_")


def _image_id(component: str, platform: str) -> str:
    return "sha256:" + (component + platform.split("/")[1]).encode().hex().ljust(64, "a")[:64]


def _pushed_digest(component: str, platform: str) -> str:
    return "sha256:" + (platform.split("/")[1] + component).encode().hex().ljust(64, "b")[:64]


def _extract_block(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    start = text.index(f"# --- BEGIN {name} ---")
    end = text.index(f"# --- END {name} ---", start)
    return text[start : text.index("\n", end) + 1]


def _extract_function(script: Path, name: str) -> str:
    return subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


FAKE_DOCKER = r"""#!/bin/bash
S="__STATE__"
san() { local r="$1"; r="${r//\//_}"; r="${r//:/_}"; r="${r//@/_}"; echo "$r"; }
echo "$*" >> "$S/calls"
case "$1 $2" in
  "image inspect")
    f="$S/local/$(san "$3")"
    [ -f "$f" ] || exit 1
    case "$5" in
      *Architecture*) cat "$f.arch" ;;
      *) cat "$f" ;;
    esac
    exit 0 ;;
  "buildx imagetools")
    case "$3" in
      create) exit 0 ;;
      inspect)
        ref="$4"
        if [ "$5" = "--raw" ]; then
          case "$ref" in
            *@*) f="$S/registry/manifest/$(san "${ref#*@}")" ;;
            *) f="$S/registry/index/$(san "$ref")" ;;
          esac
          [ -f "$f" ] || exit 1
          cat "$f"; exit 0
        fi
        f="$S/registry/tagdigest/$(san "$ref")"
        [ -f "$f" ] || exit 1
        cat "$f"; exit 0 ;;
    esac ;;
esac
if [ "$1" = "push" ]; then
  if grep -qxF "$2" "$S/pushfail" 2>/dev/null; then echo "denied" >&2; exit 1; fi
  echo "v9.9.9: digest: $(cat "$S/pushdigest/$(san "$2")") size: 1234"
  exit 0
fi
exit 0
"""


class World:
    """A fake local daemon + fake registry in one directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / "state"
        self.repo = root / "repo"
        self.bin = root / "bin"
        for d in (
            "local",
            "pushdigest",
            "registry/manifest",
            "registry/index",
            "registry/tagdigest",
        ):
            (self.state / d).mkdir(parents=True)
        (self.state / "calls").touch()
        (self.state / "pushfail").touch()
        (self.repo / "scripts" / "lib").mkdir(parents=True)
        self.bin.mkdir()
        fd = self.bin / "docker"
        fd.write_text(FAKE_DOCKER.replace("__STATE__", str(self.state)), encoding="utf-8")
        fd.chmod(fd.stat().st_mode | stat.S_IEXEC)
        stub = self.repo / "scripts" / "docker-build-push.sh"
        stub.write_text(
            f'#!/bin/bash\n[ "$1" = "list-platforms" ] && printf %s "{FAKE_PLATFORMS}"\nexit 0\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        sec = self.repo / "scripts" / "security-scan.sh"
        rows = "".join(f"{c}\\t{r}\\n" for c, r in REPOS.items())
        sec.write_text(
            f'#!/bin/bash\n[ "$1" = "list-repos" ] && printf "{rows}"\nexit 0\n', encoding="utf-8"
        )
        sec.chmod(sec.stat().st_mode | stat.S_IEXEC)
        shutil.copy(CHECKER, self.repo / "scripts" / "lib" / "manifest_platform_check.py")
        self.legs_file = root / "legs.tsv"
        self.build_world()

    # ---- consistent world -------------------------------------------------
    def build_world(self) -> None:
        rows = []
        for comp, plat in LEGS:
            tag = _tag(comp, plat)
            self.set_local(tag, _image_id(comp, plat), plat.split("/")[1])
            rows.append("\t".join([comp, REPOS[comp], plat, tag, _image_id(comp, plat)]))
            pd = _pushed_digest(comp, plat)
            (self.state / "pushdigest" / _san(tag)).write_text(pd)
            (self.state / "registry" / "tagdigest" / _san(tag)).write_text(pd)
            self.set_manifest(pd, config=_image_id(comp, plat))
        self.legs_file.write_text("\n".join(rows) + "\n", encoding="utf-8")
        for comp in REPOS:
            self.set_index(comp, {p: _pushed_digest(comp, p) for c, p in LEGS if c == comp})

    def set_local(self, tag: str, image_id: str, arch: str) -> None:
        (self.state / "local" / _san(tag)).write_text(image_id)
        (self.state / "local" / (_san(tag) + ".arch")).write_text(arch)

    def set_manifest(self, digest: str, *, config: str) -> None:
        (self.state / "registry" / "manifest" / _san(digest)).write_text(
            json.dumps({"schemaVersion": 2, "config": {"digest": config}, "layers": []})
        )

    def set_index(self, comp: str, by_platform: dict[str, str]) -> None:
        doc = {
            "manifests": [
                {
                    "digest": d,
                    "platform": {"os": p.split("/")[0], "architecture": p.split("/")[1]},
                }
                for p, d in by_platform.items()
            ]
        }
        (self.state / "registry" / "index" / _san(f"{REPOS[comp]}:{VERSION}")).write_text(
            json.dumps(doc)
        )

    # ---- run --------------------------------------------------------------
    def run(self, block_name: str = "push-built-legs") -> subprocess.CompletedProcess[str]:
        snippet = f"""
set -uo pipefail
VERSION="{VERSION}"
RED=""; GREEN=""; YELLOW=""; NC=""
record() {{ echo "$1|$2|$3" >> "{self.root}/records"; }}
publish_fail_out() {{ echo "FAILOUT|$1" >> "{self.root}/records"; exit "$1"; }}
CHECKER="./scripts/lib/manifest_platform_check.py"
MANIFEST_TMP="$(mktemp -d)"
{_extract_function(PUBLISH_SH, "inspect_raw")}
{_extract_block(PUBLISH_SH, block_name)}
echo REACHED_END
"""
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "OT_LEGS_FILE": str(self.legs_file),
        }
        return subprocess.run(
            ["bash", "-c", snippet], cwd=self.repo, env=env, capture_output=True, text=True
        )

    def records(self) -> dict[str, tuple[str, str]]:
        f = self.root / "records"
        out: dict[str, tuple[str, str]] = {}
        if f.exists():
            for line in f.read_text().splitlines():
                parts = line.split("|", 2)
                out[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "")
        return out

    def calls(self) -> list[str]:
        return (self.state / "calls").read_text().splitlines()

    def pushes(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("push ")]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# ───────────────────────────────────────────────────────── publish: the happy path ──


def test_publish_pushes_each_built_leg_and_assembles_from_pushed_digests(world: World) -> None:
    proc = world.run()
    assert proc.returncode == 0 and "REACHED_END" in proc.stdout, proc.stderr

    assert sorted(world.pushes()) == sorted(f"push {_tag(c, p)}" for c, p in LEGS)
    creates = [c for c in world.calls() if c.startswith("buildx imagetools create")]
    assert len(creates) == 3, creates
    for comp in REPOS:
        (line,) = [c for c in creates if f"--tag {REPOS[comp]}:{VERSION} " in c + " "]
        for c2, p in LEGS:
            if c2 == comp:
                assert f"{REPOS[comp]}@{_pushed_digest(comp, p)}" in line, line

    # nothing built, nothing cache-busted, :latest never named
    assert not [c for c in world.calls() if c.startswith(("buildx build ", "build "))]
    assert all("--no-cache" not in c and "latest" not in c for c in world.calls())
    rec = world.records()
    for cid in (
        "built-legs-match-ledger",
        "images-pushed",
        "registry-digest-matches-pushed",
        "registry-leg-matches-built-image",
    ):
        assert rec[cid][0] == "pass", (cid, rec.get(cid))


# ───────────────────────────────────────────── publish: refusals (each shown red) ──


def test_a_changed_local_image_id_refuses_before_any_push_naming_the_leg(world: World) -> None:
    tag = _tag("lite", "linux/arm64")
    world.set_local(tag, "sha256:" + "f" * 64, "arm64")

    proc = world.run()

    assert proc.returncode == 1
    assert world.pushes() == [], "a mismatch on ONE leg must publish NOTHING"
    status, detail = world.records()["built-legs-match-ledger"]
    assert status == "fail" and tag in detail, detail
    assert "images-pushed" not in world.records()


def test_a_leg_missing_locally_refuses_before_any_push(world: World) -> None:
    (world.state / "local" / _san(_tag("backend", "linux/amd64"))).unlink()
    proc = world.run()
    assert proc.returncode == 1
    assert world.pushes() == []
    assert "no longer present locally" in world.records()["built-legs-match-ledger"][1]


def test_a_declared_leg_absent_from_the_build_record_refuses(world: World) -> None:
    rows = [
        r
        for r in world.legs_file.read_text().splitlines()
        if not (r.startswith("frontend") and "linux/arm64" in r)
    ]
    world.legs_file.write_text("\n".join(rows) + "\n")
    proc = world.run()
    assert proc.returncode == 1
    assert world.pushes() == []
    assert (
        "frontend linux/arm64: not in the build record"
        in (world.records()["built-legs-match-ledger"][1])
    )


def test_no_build_record_is_could_not_check_exit_3(world: World) -> None:
    world.legs_file.unlink()
    proc = world.run()
    assert proc.returncode == 3
    assert world.pushes() == []
    assert world.records()["built-legs-match-ledger"][0] == "not-measured"


def test_a_failed_push_stops_and_assembles_nothing(world: World) -> None:
    failing = _tag("frontend", "linux/amd64")
    (world.state / "pushfail").write_text(failing + "\n")
    proc = world.run()
    assert proc.returncode == 1
    assert world.records()["images-pushed"][0] == "fail"
    assert failing in world.records()["images-pushed"][1]
    assert not [c for c in world.calls() if c.startswith("buildx imagetools create")]


def test_registry_tag_digest_differing_from_pushed_fails(world: World) -> None:
    tag = _tag("lite", "linux/amd64")
    (world.state / "registry" / "tagdigest" / _san(tag)).write_text("sha256:" + "c" * 64)
    proc = world.run()
    assert proc.returncode == 1
    assert world.records()["registry-digest-matches-pushed"][0] == "fail"
    assert tag in world.records()["registry-digest-matches-pushed"][1]


def test_index_listing_a_different_digest_than_pushed_fails(world: World) -> None:
    world.set_index(
        "lite",
        {
            "linux/amd64": _pushed_digest("lite", "linux/amd64"),
            "linux/arm64": "sha256:" + "d" * 64,
        },
    )
    proc = world.run()
    assert proc.returncode == 1
    assert world.records()["registry-digest-matches-pushed"][0] == "fail"


def test_registry_config_differing_from_the_built_image_fails(world: World) -> None:
    world.set_manifest(_pushed_digest("lite", "linux/arm64"), config="sha256:" + "e" * 64)
    proc = world.run()
    assert proc.returncode == 1
    assert world.records()["registry-leg-matches-built-image"][0] == "fail"
    assert world.records()["registry-digest-matches-pushed"][0] == "pass"


def test_containerd_store_where_manifest_digest_is_the_image_id_passes(world: World) -> None:
    """Push preserves the manifest there, so the pushed digest IS the local `.Id`."""
    comp, plat = "backend", "linux/amd64"
    tag = _tag(comp, plat)
    same = _pushed_digest(comp, plat)
    world.set_local(tag, same, "amd64")
    rows = [
        "\t".join([c, REPOS[c], p, _tag(c, p), same if (c, p) == (comp, plat) else _image_id(c, p)])
        for c, p in LEGS
    ]
    world.legs_file.write_text("\n".join(rows) + "\n")
    world.set_manifest(same, config="sha256:" + "9" * 64)
    proc = world.run()
    assert proc.returncode == 0, proc.stderr
    assert world.records()["registry-leg-matches-built-image"][0] == "pass"


def test_an_unreadable_registry_is_not_measured_not_a_pass(world: World) -> None:
    (world.state / "registry" / "index" / _san(f"{REPOS['frontend']}:{VERSION}")).unlink()
    proc = world.run()
    assert proc.returncode == 1
    assert world.records()["registry-digest-matches-pushed"][0] == "not-measured"


# ───────────────────────────────────────────────────── static: never rebuild/relatest ──


def test_publish_never_rebuilds_rescans_or_touches_latest() -> None:
    text = PUBLISH_SH.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "docker-build-push.sh all" not in code and "BUILD_MODE=push" not in code
    assert "--no-cache" not in code
    assert "docker-build-push.sh" in code  # only the read-only list-platforms query
    for line in code.splitlines():
        if "docker-build-push.sh" in line:
            assert "list-platforms" in line, line
        if "imagetools create" in line:
            assert '--tag "${COMP_REPO[$a_component]}:${VERSION}"' in line, line


# ───────────────────────────────────────────────────── 40-build: recording the IDs ──


def _run_build_block(
    root: Path, *, arch_for: dict[str, str], missing: set[str] | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    w = World(root)
    for comp, plat in LEGS:
        tag = _tag(comp, plat)
        arch = arch_for.get(tag, plat.split("/")[1])
        w.set_local(tag, _image_id(comp, plat), arch)
        if missing and tag in missing:
            (w.state / "local" / _san(tag)).unlink()
    out = root / "recorded.tsv"
    snippet = f"""
set -uo pipefail
VERSION="{VERSION}"
RED=""; GREEN=""; NC=""
record() {{ echo "$1|$2|$3" >> "{root}/records"; }}
build_fail_out() {{ echo "FAILOUT|$1" >> "{root}/records"; exit "$1"; }}
declare -A ALL_REPO_FOR_COMPONENT=([backend]=test/ot-backend [frontend]=test/ot-frontend [lite]=test/ot-lite)
{_extract_block(BUILD_SH, "leg-image-ids")}
echo REACHED_END
"""
    env = {**os.environ, "PATH": f"{w.bin}:{os.environ['PATH']}", "OT_LEGS_FILE": str(out)}
    proc = subprocess.run(
        ["bash", "-c", snippet], cwd=w.repo, env=env, capture_output=True, text=True
    )
    return proc, out


def test_build_records_every_legs_id_and_publish_accepts_the_record(tmp_path: Path) -> None:
    proc, out = _run_build_block(tmp_path, arch_for={})
    assert proc.returncode == 0 and "REACHED_END" in proc.stdout, proc.stderr
    rows = [r.split("\t") for r in out.read_text().splitlines()]
    assert len(rows) == len(LEGS)
    for comp, repo, plat, tag, image_id in rows:
        assert (comp, plat) in LEGS and tag == _tag(comp, plat) and repo == REPOS[comp]
        assert image_id == _image_id(comp, plat)
    assert "leg-image-ids-recorded|pass|" in (tmp_path / "records").read_text()


def test_build_refuses_a_wrong_architecture_leg_and_writes_nothing(tmp_path: Path) -> None:
    bad = _tag("lite", "linux/arm64")
    proc, out = _run_build_block(tmp_path, arch_for={bad: "amd64"})
    assert proc.returncode == 1
    assert not out.exists(), "a partial record must never exist"
    assert bad in (tmp_path / "records").read_text()


def test_build_refuses_a_missing_leg(tmp_path: Path) -> None:
    gone = _tag("backend", "linux/amd64")
    proc, out = _run_build_block(tmp_path, arch_for={}, missing={gone})
    assert proc.returncode == 1 and not out.exists()
    assert "no local image ID" in (tmp_path / "records").read_text()


# ───────────────────────────────────────────────────── checker: config-digest ──


def _checker(tmp_path: Path, doc: object) -> subprocess.CompletedProcess[str]:
    f = tmp_path / "m.json"
    f.write_text(json.dumps(doc))
    return subprocess.run(
        ["python3", "-I", str(CHECKER), "config-digest", str(f)], capture_output=True, text=True
    )


def test_config_digest_prints_the_manifests_config(tmp_path: Path) -> None:
    proc = _checker(tmp_path, {"config": {"digest": "sha256:abc"}, "layers": []})
    assert proc.returncode == 0 and proc.stdout.strip() == "sha256:abc"


def test_config_digest_on_an_index_is_could_not_check(tmp_path: Path) -> None:
    proc = _checker(tmp_path, {"manifests": []})
    assert proc.returncode == 3 and "CANNOT CHECK" in proc.stdout
