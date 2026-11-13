"""ensure_nltk_corpora must use resolve_backend_image, never pull the release :latest (EFFC).

The release image is 9.6 GB; ensure_opensearch_models already resolves the deployment's image.
Run the real function against a fake ``docker`` that has only the resolved image and record
every argument it was given.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
if [ "$1" = image ] && [ "$3" = "example/opentranscribe-backend:devtag" ]; then exit 0; fi
if [ "$1" = image ]; then exit 1; fi
exit 0
"""


def test_nltk_uses_the_resolved_image_and_never_pulls(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(_DOCKER)
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "log"
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "scripts" / "download-models.py").write_text("")
    script = f'cd "{work}"; source "{REPO_ROOT}/scripts/common.sh"; ensure_nltk_corpora'
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "STUB_LOG": str(log),
        "MODEL_CACHE_DIR": str(tmp_path / "models"),
        "DOCKERHUB_USERNAME": "example",
        "OT_IMAGE_TAG": "devtag",
        "HOME": str(tmp_path),
    }
    subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text()
    assert "pull" not in calls, calls
    assert "davidamacey/opentranscribe-backend:latest" not in calls, calls
    assert "example/opentranscribe-backend:devtag" in calls, calls
