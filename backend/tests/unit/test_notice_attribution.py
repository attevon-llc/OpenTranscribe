"""NOTICE must actually reach a deployment and stay a true map of what ships (issue #663).

Two ways this file can silently lie:

1. It reaches no deployment. A CC-BY-4.0 / Apache-2.0 attribution file that is not in
   release-manifest.txt and not COPY'd by any Dockerfile is not "distributed" to anyone —
   neither backend Dockerfile can COPY it anyway (their build context is ./backend /
   ./backend, not the repo root, so a root-level NOTICE is outside the Docker build
   context entirely), which makes the manifest entry the ONLY shipping mechanism for a
   self-hosted install.
2. It stops crediting a component the repo actually ships. #663's own audit found the
   original NOTICE credited only pyannote and omitted speakrs (Apache-2.0, the diarization
   engine now vendored inside diar-native), WhisperX, faster-whisper/CTranslate2, and ONNX
   Runtime.

This is a static scan in the same style as scripts/audit-tests.py: a must-fire case (the
guard trips on a mutated tree) and a must-stay-clean case (the real tree passes).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NOTICE = REPO_ROOT / "NOTICE"
MANIFEST = REPO_ROOT / "release-manifest.txt"
DOCKERFILE_PROD = REPO_ROOT / "backend" / "Dockerfile.prod"
DOCKERFILE_LITE = REPO_ROOT / "backend" / "Dockerfile.lite"

# Components #663's audit found actually ship / run at inference time. Every one must be
# named (case-insensitively) somewhere in NOTICE, or a real component's attribution has
# quietly dropped off the file again.
REQUIRED_CREDITS = {
    "pyannote": r"pyannote",
    "speakrs (Apache-2.0 diarization engine vendored in diar-native)": r"speakrs",
    "WhisperX": r"whisperx",
    "faster-whisper / CTranslate2": r"faster-whisper|ctranslate2",
    "ONNX Runtime": r"onnx runtime|onnxruntime",
}


def _manifest_entries() -> list[tuple[str, set[str]]]:
    """Parse release-manifest.txt the same way its own test suite does."""
    entries: list[tuple[str, set[str]]] = []
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        path = parts[0].strip()
        flags = set()
        if len(parts) > 1 and parts[1].strip():
            flags = {f.strip() for f in parts[1].split(",") if f.strip()}
        entries.append((path, flags))
    return entries


@pytest.mark.skipif(not NOTICE.exists(), reason="NOTICE not present in this checkout")
def test_notice_exists_and_is_not_empty():
    assert NOTICE.read_text(encoding="utf-8").strip(), "NOTICE exists but is empty"


@pytest.mark.skipif(
    not (NOTICE.exists() and MANIFEST.exists()),
    reason="NOTICE or release-manifest.txt not present in this checkout",
)
def test_notice_is_listed_in_release_manifest_and_required():
    """The manifest is the only mechanism that gets a repo-root file to a self-hosted
    install downloaded via setup-opentranscribe.sh / update-full. Neither backend
    Dockerfile can COPY a repo-root file (their build context is ./backend), so this is
    not one mechanism among several -- it is the only one available to those consumers.
    """
    entries = dict(_manifest_entries())
    assert "NOTICE" in entries, (
        "NOTICE is not in release-manifest.txt -- a self-hosted install downloaded via "
        "setup-opentranscribe.sh or `opentranscribe.sh update-full` never receives the "
        "CC-BY-4.0 / Apache-2.0 attribution file at all"
    )
    assert "optional" not in entries["NOTICE"], (
        "NOTICE is marked optional in release-manifest.txt -- an optional 404 degrades "
        "silently (a yellow 'not in this release' line) and the install continues with "
        "no attribution file, which defeats the point of shipping it"
    )


@pytest.mark.skipif(
    not (DOCKERFILE_PROD.exists() and DOCKERFILE_LITE.exists()),
    reason="backend Dockerfiles not present in this checkout",
)
def test_backend_dockerfiles_build_context_cannot_reach_root_notice():
    """Documents WHY the manifest entry above is required, not merely sufficient.

    Both backend Dockerfiles COPY the full build context (`COPY --chown=... . .`), but
    docker-compose.prod.yml scopes that context to ./backend -- a repo-root NOTICE is
    never inside it. If this ever changes (e.g. the compose context widens to the repo
    root), a Dockerfile COPY becomes a second valid shipping mechanism and this test
    should be revisited rather than treated as a permanent constraint.
    """
    prod_text = DOCKERFILE_PROD.read_text(encoding="utf-8")
    lite_text = DOCKERFILE_LITE.read_text(encoding="utf-8")
    for name, text in (("Dockerfile.prod", prod_text), ("Dockerfile.lite", lite_text)):
        assert not re.search(r"COPY[^\n]*\bNOTICE\b", text), (
            f"{name} now COPYs NOTICE directly -- if the build context was widened to "
            "reach the repo root, that's a legitimate second shipping mechanism, but "
            "this test (and the manifest requirement above) needs updating to match, "
            "not silently coexisting with an assumption that is no longer true"
        )


@pytest.mark.skipif(not NOTICE.exists(), reason="NOTICE not present in this checkout")
@pytest.mark.parametrize("component,pattern", sorted(REQUIRED_CREDITS.items()))
def test_notice_credits_every_shipped_component(component: str, pattern: str):
    text = NOTICE.read_text(encoding="utf-8")
    assert re.search(pattern, text, re.IGNORECASE), (
        f"NOTICE no longer credits {component} -- issue #663's audit found the original "
        f"NOTICE credited only pyannote and silently dropped every other component the "
        f"repo actually ships/runs at inference time"
    )
