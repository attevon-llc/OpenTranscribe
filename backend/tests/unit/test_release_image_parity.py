"""TR-P: image_parity.py's pure logic, plus the wiring of the three stages that use it.

The docker-touching halves (`record-tested`, `check-shipped`) need images and are exercised by the
release wave run; what must not regress silently is the fingerprint definition and the gates.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
REL = REPO / "scripts" / "release"
_spec = importlib.util.spec_from_file_location("image_parity", REL / "image_parity.py")
assert _spec and _spec.loader
ip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ip)

RULES = ip.load_dockerignore(
    "tests/\n*.md\n**/.env\nscripts/\n!scripts/api-start.sh\n__pycache__/\n"
)


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        ("tests/unit/test_x.py", True),
        ("app/main.py", False),
        ("README.md", True),
        ("app/README.md", False),  # Docker: `*.md` has no `**`, so it is root-only
        ("app/sub/.env", True),
        ("scripts/foo.sh", True),
        ("scripts/api-start.sh", False),
        ("app/__pycache__/x.pyc", False),  # root-only, as Docker reads it
    ],
)
def test_dockerignore_follows_docker_semantics(path: str, ignored: bool) -> None:
    assert ip.is_ignored(path, RULES) is ignored


def test_compare_names_each_kind_of_difference() -> None:
    same = {"pip_freeze_sha": "a", "app_tree_sha": "b"}
    assert ip.compare(same, dict(same)) == []
    pip = ip.compare(same, {**same, "pip_freeze_sha": "z"})
    app = ip.compare(same, {**same, "app_tree_sha": "z"})
    assert len(pip) == 1 and "pip freeze" in pip[0]
    assert len(app) == 1 and "/app tree" in app[0]


def test_sha_is_order_independent_only_via_sorted_input() -> None:
    assert ip.sha_of(["a", "b"]) != ip.sha_of(["b", "a"])


def test_checkout_manifest_matches_real_dockerignore() -> None:
    paths = ip.tracked_paths(REPO)
    assert "app/main.py" in paths
    assert not any(p.startswith("tests/") for p in paths)  # L1 reuse relies on this exclusion


def test_criteria_declared_and_wired() -> None:
    crit = yaml.safe_load((REL / "release-criteria.yaml").read_text())["stages"]
    ids = {st: {c["id"] for c in crit[st]["criteria"]} for st in ("test", "tag")}
    assert "tested-images-recorded" in ids["test"]
    assert "shipped-python-equals-tested" in ids["tag"]
    assert "record tested-images-recorded" in (REL / "60-test.sh").read_text()
    tag = (REL / "70-tag.sh").read_text()
    assert "record shipped-python-equals-tested" in tag
    # the parity gate sits after the evidence loop and before the tag is created
    assert (
        tag.index("END artifacts-match-tagged-commit")
        < tag.index("record shipped-python-equals-tested")
        < tag.index("git tag -a")
    )


def test_missing_fingerprint_is_not_measured_not_pass(tmp_path: Path) -> None:
    tag = (REL / "70-tag.sh").read_text()
    block = tag[tag.index('[[ ! -s "$parity_dir/tested-fingerprint.txt"') :]
    assert "not-measured" in block.split("fi\n", 1)[0]


def test_scan_never_defaults_into_tracked_reports() -> None:
    scan = (REL / "50-scan.sh").read_text()
    assert 'OUT_DIR="${OT_SCAN_DIR:-./.release/${VERSION}/scan-reports}"' in scan
    assert "./security-reports}" not in scan.replace("security-reports/*", "")
    ignored = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", ".release/v9.9.9/scan-reports/x-trivy.txt"]
    )
    assert ignored.returncode == 0


def test_publish_reports_copies_only_the_tracked_set(tmp_path: Path) -> None:
    src, dest = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    for n in (
        "a-trivy.txt",
        "a-dockle.json",
        "a-trivy.json",
        "a-sbom.json",
        "a-grype.json",
        "a-sbom.txt",
    ):
        (src / n).write_text("x")
    out = subprocess.run(
        [
            "bash",
            "-c",
            f'source {REL}/release-assets.sh; release_assets_publish_reports "{src}" "{dest}"',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "3"
    assert sorted(p.name for p in dest.iterdir()) == ["a-dockle.json", "a-sbom.txt", "a-trivy.txt"]


def test_host_concurrency_is_opt_in_and_fail_closed() -> None:
    build = (REL / "40-build.sh").read_text()
    assert '"${OT_BUILD_HOST_CONCURRENT:-false}" == "true"' in build
    assert 'wait "${batch_pids[$j]}"' in build
