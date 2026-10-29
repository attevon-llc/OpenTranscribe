"""scripts/dependency-diff.py and the parsers in scripts/lib/dependency_*.py.

Two layers. Parser tests feed manifest text straight in. End-to-end tests build a throwaway git
repo, commit two states and run the real ``main`` against the two refs, so they exercise the
``git show`` path, the flags, the exit codes and the files written.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import dependency_images as images
import dependency_inventory as inv
import dependency_model as model


def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dependency_diff_under_test", ROOT / "scripts" / "dependency-diff.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


# --- a throwaway repo ------------------------------------------------------------------------

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.path, env=GIT_ENV, capture_output=True, text=True, check=True
        )
        return proc.stdout.strip()

    def commit(self, files: dict[str, str | None]) -> str:
        """Write (or delete, for ``None``) files and commit; returns the commit sha."""
        for rel, content in files.items():
            target = self.path / rel
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "state", "--allow-empty")
        return self._git("rev-parse", "HEAD")

    def run(self, base: str, head: str, *extra: str) -> tuple[int, str, dict[str, object]]:
        out = self.path.parent / f"out-{self.path.name}"
        code = cli.main(["--repo", str(self.path), "--base", base, "--head", head,
                         "--out-dir", str(out), *extra])  # fmt: skip
        md = (
            (out / "dependency-diff.md").read_text()
            if (out / "dependency-diff.md").exists()
            else ""
        )
        data = json.loads((out / "dependency-diff.json").read_text()) if md else {}
        return code, md, data


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "work")


def _lock(packages: dict[str, dict[str, object]], root: dict[str, object] | None = None) -> str:
    entries: dict[str, object] = {"": root or {}}
    entries.update({f"node_modules/{n}": info for n, info in packages.items()})
    return json.dumps({"lockfileVersion": 3, "packages": entries})


def _pkg(deps: dict[str, str] | None = None, dev: dict[str, str] | None = None) -> str:
    return json.dumps({"name": "x", "dependencies": deps or {}, "devDependencies": dev or {}})


def _rows(data: dict[str, object], path: str, kind: str | None = None) -> list[dict[str, object]]:
    sections = data["sections"]
    assert isinstance(sections, list)
    found = [s for s in sections if s["path"] == path and (kind is None or s["kind"] == kind)]
    assert len(found) == 1, f"expected one section for {path}, got {[s['path'] for s in sections]}"
    return cast(list[dict[str, object]], found[0]["rows"])


def _by_name(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(r["name"]): r for r in rows}


# --- python parsing --------------------------------------------------------------------------


def test_local_version_tag_is_kept_verbatim() -> None:
    deps = inv.parse_requirements("torch==2.11.0+cu128\n")
    assert deps["torch"].version == "2.11.0+cu128"


def test_extra_index_url_is_a_tracked_entry() -> None:
    deps = inv.parse_requirements("--extra-index-url https://download.pytorch.org/whl/cu128\n")
    (dep,) = deps.values()
    assert dep.name == "--extra-index-url https://download.pytorch.org/whl/cu128"
    assert dep.direct is None


def test_same_name_with_different_markers_are_two_entries() -> None:
    deps = inv.parse_requirements(
        "onnxruntime-gpu==1.30.0; sys_platform == 'linux'\n"
        "onnxruntime-gpu==1.29.0; sys_platform != 'linux'\n"
    )
    assert set(deps) == {
        "onnxruntime-gpu;sys_platform=='linux'",
        "onnxruntime-gpu;sys_platform!='linux'",
    }


def test_git_and_url_pins_are_tracked_by_their_reference() -> None:
    deps = inv.parse_requirements(
        "pyannote.audio @ git+https://github.com/o/pyannote-audio.git@e3924b4\n"
        "en_core_web_sm @ https://example.invalid/en_core_web_sm-3.8.0-py3-none-any.whl\n"
        "-e git+https://github.com/o/tool.git@abc#egg=tool\n"
    )
    assert deps["pyannote-audio"].version == "git+https://github.com/o/pyannote-audio.git@e3924b4"
    assert deps["en-core-web-sm"].version.endswith("py3-none-any.whl")
    assert deps["tool"].version == "git+https://github.com/o/tool.git@abc"
    assert all(d.versions == () for d in deps.values()), "a commit is not a version number"


def test_requirements_include_is_an_entry_and_hashes_comments_are_ignored() -> None:
    deps = inv.parse_requirements(
        "-r requirements.txt\n# note\nrequests==2.0.0 \\\n    --hash=sha256:abc  # why\n"
    )
    assert deps["include requirements.txt"].version == "include"
    assert deps["requests"].version == "2.0.0"


def test_nodeps_role_is_the_scope_of_the_files_pins() -> None:
    deps = inv.parse_requirements("whisperx==3.8.6\n", "--no-deps")
    assert deps["whisperx"].scope == "--no-deps"


def test_garbage_line_fails_closed() -> None:
    with pytest.raises(model.ManifestError, match="unparsable requirement"):
        inv.parse_requirements("requests==1\n=== not a requirement ===\n")


# --- version rules ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("1.9.0", "2.0.0", True),
        ("0.3.1", "0.4.0", True),  # minor change below 1.0
        ("0.3.1", "0.3.2", False),
        ("1.2.0", "1.3.0", False),
        ("2.11.0+cu128", "2.11.0+cpu", False),
        ("^26.5.1", "^27.0.0", True),
        ("0.63.4", "1.0.0", True),
    ],
)
def test_major_rule(old: str, new: str, expected: bool) -> None:
    assert model.is_major((old,), (new,)) is expected


# --- npm parsing -----------------------------------------------------------------------------


def test_npm_lock_direct_transitive_and_dev_flags() -> None:
    text = _lock(
        {
            "a": {"version": "1.0.0", "license": "MIT"},
            "b": {"version": "2.0.0", "dev": True, "license": "ISC"},
            "c": {"version": "3.0.0", "dev": True},
        }
    )
    deps = inv.parse_npm_lock("l.json", text, direct={"a", "b"})
    assert (deps["a"].direct, deps["a"].scope, deps["a"].license) == (True, "prod", "MIT")
    assert (deps["b"].direct, deps["b"].scope) == (True, "dev")
    assert (deps["c"].direct, deps["c"].scope) == (False, "dev")


def test_npm_name_installed_twice_is_prod_unless_every_copy_is_dev() -> None:
    text = json.dumps(
        {
            "packages": {
                "": {},
                "node_modules/x": {"version": "1.0.0", "dev": True},
                "node_modules/y/node_modules/x": {"version": "2.0.0"},
            }
        }
    )
    dep = inv.parse_npm_lock("l.json", text, direct=set())["x"]
    assert dep.version == "1.0.0, 2.0.0"
    assert dep.scope == "prod"


def test_npm_lock_that_is_truncated_json_fails_closed() -> None:
    with pytest.raises(model.ManifestError, match="not valid JSON"):
        inv.parse_npm_lock("l.json", _lock({"a": {"version": "1.0.0"}})[:40])


def test_npm_lock_without_packages_or_dependencies_fails_closed() -> None:
    with pytest.raises(model.ManifestError, match="neither"):
        inv.parse_npm_lock("l.json", json.dumps({"lockfileVersion": 3}))


def test_package_json_with_a_non_map_section_fails_closed() -> None:
    with pytest.raises(model.ManifestError, match="devDependencies"):
        inv.parse_package_json("p.json", json.dumps({"devDependencies": ["x"]}))


# --- images ----------------------------------------------------------------------------------

DOCKERFILE = """\
ARG PY=3.13
FROM python:${PY}-slim AS builder
FROM --platform=$BUILDPLATFORM node:26-alpine AS build
FROM ghcr.io/o/tool@sha256:aaaa AS bin-amd64
FROM bin-${TARGETARCH} AS bin
FROM builder AS again
FROM scratch
FROM nginx:1.31.2-alpine
"""


def test_dockerfile_keeps_external_images_and_skips_internal_stages() -> None:
    deps = images.parse_dockerfile("Dockerfile", DOCKERFILE)
    assert {k: d.version for k, d in deps.items()} == {
        "FROM builder": "python:3.13-slim",
        "FROM build": "node:26-alpine",
        "FROM bin-amd64": "ghcr.io/o/tool@sha256:aaaa",
        "FROM #1": "nginx:1.31.2-alpine",
    }
    assert deps["FROM build"].versions == ("26-alpine",)
    assert deps["FROM bin-amd64"].versions == ()


def test_compose_image_is_keyed_by_service_and_keeps_interpolation() -> None:
    text = (
        "services:\n"
        "  backend:\n"
        "    build: .\n"
        "    image: ${BACKEND_IMAGE:-me/backend:${TAG:-latest}}  # note\n"
        "  db:\n"
        "    image: 'postgres:16-alpine'\n"
        "x-other:\n"
        "  image: busybox:1\n"
    )
    deps = images.parse_compose("compose.yml", text)
    assert {k: d.version for k, d in deps.items()} == {
        "services/backend": "${BACKEND_IMAGE:-me/backend:${TAG:-latest}}",
        "services/db": "postgres:16-alpine",
        "x-other": "busybox:1",
    }
    assert deps["services/backend"].versions == ()


# --- diff rows and flags ---------------------------------------------------------------------


def _dep(name: str, version: str, lic: str | None = None) -> model.Dep:
    return model.Dep(name, version, (version,), license=lic)


def test_added_removed_changed_and_flags() -> None:
    old = {"a": _dep("a", "1.0.0", "MIT"), "gone": _dep("gone", "1.0.0")}
    new = {
        "a": _dep("a", "2.0.0", "GPL-3.0"),
        "fresh": _dep("fresh", "0.1.0", "MIT"),
    }
    rows = _by_name([r.__dict__ for r in cli.diff_deps(old, new)])
    assert rows["fresh"]["flags"] == ["NEW"]
    assert rows["fresh"]["license_class"] == "OK"
    assert rows["gone"]["change"] == "removed"
    assert rows["a"]["flags"] == ["MAJOR", "LICENSE-CHANGED"]


def test_new_without_a_known_licence_has_no_class() -> None:
    (row,) = cli.diff_deps({}, {"x": _dep("x", "1.0.0")})
    assert row.flags == ["NEW"]
    assert row.license_class is None


def test_unchanged_dependency_produces_no_row() -> None:
    same = {"a": _dep("a", "1.0.0")}
    assert cli.diff_deps(same, dict(same)) == []


def test_same_version_with_a_new_source_is_reported() -> None:
    old = {"a": model.Dep("a", "1.0.0", ("1.0.0",), source="|u1|sha1")}
    new = {"a": model.Dep("a", "1.0.0", ("1.0.0",), source="|u2|sha2")}
    (row,) = cli.diff_deps(old, new)
    assert row.notes == ["same version, source changed"]


# --- end to end: npm -------------------------------------------------------------------------


def test_lockfile_pair_add_remove_change_direct_and_dev(repo: Repo) -> None:
    base = repo.commit(
        {
            "web/package.json": _pkg({"keep": "^1.0.0", "old": "^1.0.0"}, {"tool": "^1.0.0"}),
            "web/package-lock.json": _lock(
                {
                    "keep": {"version": "1.0.0", "license": "MIT"},
                    "old": {"version": "1.0.0"},
                    "tool": {"version": "1.4.0", "dev": True},
                    "inner": {"version": "0.2.0", "dev": True},
                },
                {"dependencies": {"keep": "^1.0.0", "old": "^1.0.0"}},
            ),
        }
    )
    head = repo.commit(
        {
            "web/package.json": _pkg({"keep": "^2.0.0", "added": "^1.0.0"}, {"tool": "^2.0.0"}),
            "web/package-lock.json": _lock(
                {
                    "keep": {"version": "2.0.0", "license": "GPL-3.0"},
                    "added": {"version": "1.0.0", "license": "MIT"},
                    "tool": {"version": "2.0.0", "dev": True},
                    "inner": {"version": "0.3.0", "dev": True},
                    "newdev": {"version": "1.0.0", "dev": True},
                }
            ),
        }
    )
    code, md, data = repo.run(base, head)
    assert code == 0
    rows = _by_name(_rows(data, "web/package-lock.json"))
    assert rows["added"]["change"] == "added" and rows["added"]["flags"] == ["NEW"]
    assert (rows["added"]["direct"], rows["added"]["scope"]) == (True, "prod")
    assert rows["added"]["license_class"] == "OK"
    assert rows["old"]["change"] == "removed"
    assert rows["keep"]["flags"] == ["MAJOR", "LICENSE-CHANGED"]
    assert (rows["tool"]["direct"], rows["tool"]["scope"]) == (True, "dev")
    assert rows["tool"]["flags"] == ["MAJOR"]
    assert (rows["inner"]["direct"], rows["inner"]["scope"]) == (False, "dev")
    assert rows["inner"]["flags"] == ["MAJOR"], "0.2 -> 0.3 is a minor change below 1.0"
    assert (rows["newdev"]["direct"], rows["newdev"]["scope"]) == (False, "dev")
    declared = _by_name(_rows(data, "web/package.json", "declared"))
    assert declared["keep"]["old"] == "^1.0.0" and declared["keep"]["new"] == "^2.0.0"
    assert "**MAJOR**" in md and "**NEW**" in md and "**LICENSE-CHANGED**" in md


def test_alias_retarget_at_the_same_version_is_a_change(repo: Repo) -> None:
    def lock(target: str) -> str:
        return _lock({"pretty": {"version": "1.0.0", "name": target, "integrity": "sha-1"}})

    base = repo.commit(
        {"package.json": _pkg({"pretty": "npm:a@1"}), "package-lock.json": lock("a")}
    )
    head = repo.commit({"package-lock.json": lock("b")})
    code, _, data = repo.run(base, head)
    assert code == 0
    (row,) = _rows(data, "package-lock.json")
    assert row["notes"] == ["same version, source changed"]


def test_unparsable_lockfile_exits_two_and_writes_no_report(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _lock({"a": {"version": "1.0.0"}})
    base = repo.commit({"package-lock.json": good})
    head = repo.commit({"package-lock.json": good[: len(good) // 2]})
    code, md, _ = repo.run(base, head)
    assert code == 2
    assert md == "", "an unreadable manifest must not produce a report at all"
    assert "package-lock.json" in capsys.readouterr().err


def test_unparsable_base_side_also_exits_two(repo: Repo) -> None:
    base = repo.commit({"package.json": "{not json"})
    head = repo.commit({"package.json": _pkg({"a": "^1"})})
    assert repo.run(base, head)[0] == 2


def test_unknown_ref_exits_two(repo: Repo) -> None:
    head = repo.commit({"package.json": _pkg()})
    assert repo.run("no-such-ref", head)[0] == 2


# --- end to end: python ----------------------------------------------------------------------


def test_python_requirements_pins_markers_includes_index_and_nodeps(repo: Repo) -> None:
    base = repo.commit(
        {
            "backend/requirements.txt": "fastapi==0.136.0\ntorch==2.10.0+cu128\n"
            "--extra-index-url https://download.pytorch.org/whl/cu128\n"
            "onnxruntime==1.0; sys_platform == 'darwin'\n",
            "backend/requirements-dev.txt": "-r requirements.txt\npytest==9.0.0\n",
            "backend/requirements-nodeps.txt": "whisperx==3.8.5\n",
        }
    )
    head = repo.commit(
        {
            "backend/requirements.txt": "fastapi==0.136.1\ntorch==2.11.0+cu128\n"
            "--extra-index-url https://download.pytorch.org/whl/cu129\n"
            "onnxruntime==1.0; sys_platform == 'darwin'\n"
            "onnxruntime==2.0; sys_platform == 'linux'\n",
            "backend/requirements-dev.txt": "-r requirements.txt\npytest==10.0.0\nruff==0.16.7\n",
            "backend/requirements-nodeps.txt": "whisperx==3.8.6\n",
        }
    )
    code, _, data = repo.run(base, head)
    assert code == 0
    main = _by_name(_rows(data, "backend/requirements.txt"))
    assert main["fastapi"]["flags"] == []
    assert main["torch"]["old"] == "2.10.0+cu128" and main["torch"]["new"] == "2.11.0+cu128"
    assert main["torch"]["flags"] == [], "2.10 -> 2.11 is not below 1.0"
    assert main["onnxruntime"]["new"] == "2.0" and main["onnxruntime"]["change"] == "added"
    index = [n for n in main if n.startswith("--extra-index-url")]
    assert sorted((main[n]["change"], n.rsplit("/", 1)[-1]) for n in index) == [
        ("added", "cu129"),
        ("removed", "cu128"),
    ]
    dev = _by_name(_rows(data, "backend/requirements-dev.txt"))
    assert dev["pytest"]["flags"] == ["MAJOR"]
    assert dev["ruff"]["flags"] == ["NEW"] and dev["ruff"]["direct"] is True
    assert "-r requirements.txt" not in dev, "an unchanged include is not a change"
    nodeps = _by_name(_rows(data, "backend/requirements-nodeps.txt"))
    assert nodeps["whisperx"]["scope"] == "--no-deps"


def test_marker_duplicate_change_is_attributed_to_the_right_marker(repo: Repo) -> None:
    both = "torch==1.0; sys_platform == 'darwin'\ntorch==2.0; sys_platform == 'linux'\n"
    base = repo.commit({"backend/requirements.txt": both})
    head = repo.commit({"backend/requirements.txt": both.replace("torch==2.0", "torch==3.0")})
    _, _, data = repo.run(base, head)
    (row,) = _rows(data, "backend/requirements.txt")
    assert (row["old"], row["new"], row["flags"]) == ("2.0", "3.0", ["MAJOR"])


def test_git_url_pin_swap_is_a_change(repo: Repo) -> None:
    line = "pyannote.audio @ git+https://github.com/o/p.git@{}\n"
    base = repo.commit({"backend/requirements.txt": line.format("aaa111")})
    head = repo.commit({"backend/requirements.txt": line.format("bbb222")})
    _, _, data = repo.run(base, head)
    (row,) = _rows(data, "backend/requirements.txt")
    assert str(row["old"]).endswith("@aaa111") and str(row["new"]).endswith("@bbb222")


def test_include_of_an_untracked_file_fails_closed(repo: Repo) -> None:
    base = repo.commit({"backend/requirements-dev.txt": "pytest==9.0.0\n"})
    head = repo.commit({"backend/requirements-dev.txt": "-r gone.txt\npytest==9.0.0\n"})
    assert repo.run(base, head)[0] == 2


def test_included_file_with_an_unusual_name_is_diffed(repo: Repo) -> None:
    base = repo.commit(
        {"backend/requirements.txt": "-r shared/base.in\n", "backend/shared/base.in": "a==1.0.0\n"}
    )
    head = repo.commit({"backend/shared/base.in": "a==1.0.0\nb==1.0.0\n"})
    _, _, data = repo.run(base, head)
    assert _by_name(_rows(data, "backend/shared/base.in"))["b"]["flags"] == ["NEW"]


# --- end to end: images ----------------------------------------------------------------------


def test_dockerfile_digest_change_and_compose_image_change(repo: Repo) -> None:
    def dockerfile(digest: str) -> str:
        return f"FROM ghcr.io/o/diar@sha256:{digest} AS bin\nFROM node:26-alpine\n"

    def compose(tag: str) -> str:
        return f"services:\n  web:\n    image: nginx:{tag}\n"

    base = repo.commit(
        {"backend/Dockerfile.prod": dockerfile("aaa"), "docker-compose.yml": compose("1.30.0")}
    )
    head = repo.commit(
        {"backend/Dockerfile.prod": dockerfile("bbb"), "docker-compose.yml": compose("2.0.0")}
    )
    code, md, data = repo.run(base, head)
    assert code == 0
    (docker_row,) = _rows(data, "backend/Dockerfile.prod")
    assert docker_row["name"] == "FROM bin"
    assert str(docker_row["old"]).endswith("sha256:aaa") and str(docker_row["new"]).endswith(
        "sha256:bbb"
    )
    assert docker_row["flags"] == [], "a digest is not a version number"
    (compose_row,) = _rows(data, "docker-compose.yml")
    assert (compose_row["name"], compose_row["flags"]) == ("services/web", ["MAJOR"])
    assert "## docker" in md


def test_dockerfile_with_an_unparsable_from_fails_closed(repo: Repo) -> None:
    base = repo.commit({"Dockerfile": "FROM node:26\n"})
    head = repo.commit({"Dockerfile": "FROM\n"})
    assert repo.run(base, head)[0] == 2


# --- end to end: reporting honesty -----------------------------------------------------------


def test_no_changes_message_only_when_nothing_changed(repo: Repo) -> None:
    base = repo.commit({"backend/requirements.txt": "a==1\n", "README.md": "x"})
    head = repo.commit({"README.md": "y"})
    code, md, data = repo.run(base, head)
    assert code == 0 and "No dependency changes." in md
    assert data["sections"] == [] and data["unsupported"] == []


def test_changed_unsupported_lockfile_is_listed_and_never_reads_as_no_changes(repo: Repo) -> None:
    base = repo.commit({"yarn.lock": "a@1:\n  version 1\n"})
    head = repo.commit({"yarn.lock": "a@1:\n  version 2\n"})
    code, md, data = repo.run(base, head)
    assert code == 0
    assert data["unsupported"] == ["yarn.lock"]
    assert "No dependency changes." not in md
    assert "NOT DIFFED" in md


def test_worktree_head_sees_uncommitted_edits(repo: Repo) -> None:
    base = repo.commit({"backend/requirements.txt": "a==1.0.0\n"})
    (repo.path / "backend" / "requirements.txt").write_text("a==2.0.0\nb==1.0.0\n")
    code, _, data = repo.run(base, "WORKTREE")
    assert code == 0
    rows = _by_name(_rows(data, "backend/requirements.txt"))
    assert rows["a"]["flags"] == ["MAJOR"] and rows["b"]["flags"] == ["NEW"]


# --- end to end: resolved (transitive) leg ---------------------------------------------------


def _report(**packages: tuple[str, str]) -> str:
    return json.dumps(
        {
            "install": [
                {"metadata": {"name": n, "version": v, "license_expression": lic}}
                for n, (v, lic) in packages.items()
            ]
        }
    )


def _write_reports(root: Path, reports: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in reports.items():
        (root / f"{name}.json").write_text(text)
    return root


def test_resolved_leg_reports_transitive_changes_and_leaves_direct_to_the_manifest(
    repo: Repo, tmp_path: Path
) -> None:
    reqs = "fastapi==0.136.0\n"
    base = repo.commit({"backend/requirements.txt": reqs})
    head = repo.commit({"backend/requirements.txt": reqs.replace("0.136.0", "0.136.1")})
    old = _write_reports(
        tmp_path / "old",
        {"requirements.txt": _report(fastapi=("0.136.0", "MIT"), starlette=("1.0.0", "MIT"))},
    )
    new = _write_reports(
        tmp_path / "new",
        {
            "requirements.txt": _report(
                fastapi=("0.136.1", "MIT"),
                starlette=("2.0.0", "GPL-3.0-only"),
                evilpkg=("1.0.0", "GPL-3.0-only"),
                okpkg=("1.0.0", "Apache-2.0"),
            )
        },
    )
    code, md, data = repo.run(base, head, "--resolved-base", str(old), "--resolved-head", str(new))
    assert code == 0
    resolved = _by_name(_rows(data, "backend/requirements.txt", "resolved"))
    assert "fastapi" not in resolved, "a direct pin is reported once, in the manifest section"
    assert resolved["starlette"]["flags"] == ["MAJOR", "LICENSE-CHANGED"]
    assert (resolved["starlette"]["direct"], resolved["evilpkg"]["direct"]) == (False, False)
    assert resolved["evilpkg"]["flags"] == ["NEW"]
    assert resolved["evilpkg"]["license_class"] == "BLOCKER"
    assert resolved["okpkg"]["license_class"] == "OK"
    manifest = _by_name(_rows(data, "backend/requirements.txt", "manifest"))
    assert manifest["fastapi"]["license"] == "MIT"
    assert "Transitive Python closure not compared" not in md


def test_resolved_reports_must_come_as_a_pair(repo: Repo, tmp_path: Path) -> None:
    head = repo.commit({"backend/requirements.txt": "a==1\n"})
    only_one = _write_reports(tmp_path / "x", {"requirements.txt": _report()})
    assert repo.run(head, head, "--resolved-base", str(only_one))[0] == 2


def test_unusable_pip_report_exits_two(repo: Repo, tmp_path: Path) -> None:
    head = repo.commit({"backend/requirements.txt": "a==1\n"})
    bad = _write_reports(tmp_path / "bad", {"requirements.txt": '{"version": "1"}'})
    assert repo.run(head, head, "--resolved-base", str(bad), "--resolved-head", str(bad))[0] == 2


def test_file_without_a_report_at_both_refs_is_named_as_not_compared(
    repo: Repo, tmp_path: Path
) -> None:
    base = repo.commit(
        {"backend/requirements.txt": "a==1\n", "backend/requirements-ci.txt": "b==1\n"}
    )
    head = repo.commit({"backend/requirements-ci.txt": "b==2\n"})
    old = _write_reports(tmp_path / "o", {"requirements.txt": _report(a=("1", "MIT"))})
    new = _write_reports(tmp_path / "n", {"requirements.txt": _report(a=("1", "MIT"))})
    _, md, data = repo.run(base, head, "--resolved-base", str(old), "--resolved-head", str(new))
    assert any("requirements-ci.txt" in n for n in cast(list[str], data["notes"]))
    assert "Transitive Python closure NOT compared for" in md


# --- the real tree ---------------------------------------------------------------------------


def test_real_repo_manifests_all_parse_and_cover_the_expected_files() -> None:
    inventory = inv.collect(
        model.WorktreeSource(ROOT), {"backend/requirements-nodeps.txt": ("--no-deps",)}
    )
    paths = set(inventory.manifests)
    assert len([p for p in paths if p.startswith("backend/requirements")]) == 8
    for lock in ("package-lock.json", "frontend/package-lock.json", "docs-site/package-lock.json"):
        assert lock in paths
        assert lock.replace("package-lock", "package") in paths
    assert "backend/Dockerfile.prod" in paths and "docker-compose.yml" in paths
    nodeps = inventory.manifests["backend/requirements-nodeps.txt"].deps
    assert {d.scope for d in nodeps.values()} == {"--no-deps"}
    torch = inventory.manifests["backend/requirements-lite.txt"].deps["torch"]
    assert torch.version.endswith("+cpu")
    frontend = inventory.manifests["frontend/package-lock.json"].deps
    assert any(d.direct for d in frontend.values()) and any(not d.direct for d in frontend.values())
    assert any(d.scope == "dev" for d in frontend.values())
