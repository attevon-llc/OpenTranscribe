"""Image pins in the compose files are kept current (issue #1369).

nginx in docker-compose.nginx.yml sat on 1.27 while the frontend and docs images moved to
1.31, because Dependabot's root entry used the `docker` ecosystem, which reads Dockerfiles,
and the repo root has none. Compose files need the separate `docker-compose` ecosystem.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def _from_tag(dockerfile: Path) -> str:
    tags: list[str] = re.findall(r"^FROM (nginx:\S+)", dockerfile.read_text(encoding="utf-8"), re.M)
    assert len(tags) == 1, f"{dockerfile}: {tags}"
    return tags[0]


def test_the_https_proxy_runs_the_same_nginx_as_the_frontend_and_docs_images():
    overlay = yaml.safe_load((REPO_ROOT / "docker-compose.nginx.yml").read_text(encoding="utf-8"))
    image = overlay["services"]["nginx"]["image"]
    assert image == _from_tag(REPO_ROOT / "frontend" / "Dockerfile.prod")
    assert image == _from_tag(REPO_ROOT / "docs-site" / "Dockerfile")


def test_dependabot_watches_the_root_compose_files_with_the_compose_ecosystem():
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    root = {u["package-ecosystem"] for u in config["updates"] if u["directory"] == "/"}
    assert "docker-compose" in root
    assert not list(REPO_ROOT.glob("Dockerfile*")), "fixture premise: the root has no Dockerfile"
    assert "docker" not in root, "a `docker` entry at / reads Dockerfiles, and there are none"
