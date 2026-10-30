"""Every THIRD-PARTY image in ``docker-compose*.yml`` must be pinned to a version or digest.

A floating tag (``latest``, ``stable``, ``alpine``) means the stack a developer starts today is
not the stack CI or a teammate started last month, and a registry push can change behaviour
under an unchanged repo. ``${VAR:-default}`` defaults count: the default is what runs when the
variable is unset, which is the common case.

First-party and locally-built images are exempt AS A CLASS, each with its reason in
``EXEMPT_CLASSES``. A new floating ref that matches no class fails here.

Static by construction: no docker, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``${NAME:-default}`` with no nested ``${`` inside the default, so innermost resolves first.
_VAR_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^${}]*)\}")
_IMAGE_LINE = re.compile(r"^\s*image:\s*(\S+)\s*(?:#.*)?$")

#: Tags that float even though they look like a name.
FLOATING_TAGS = {"latest", "stable", "alpine", "slim", "main", "master", "edge", "nightly"}

#: (resolved-ref regex, reason). Whole-ref match.
EXEMPT_CLASSES: list[tuple[str, str]] = [
    (
        r"davidamacey/opentranscribe-[a-z-]+:(latest|blackwell)",
        "First-party image. The tag is chosen by the release/installer tooling through "
        "OT_IMAGE_TAG / OT_BLACKWELL_IMAGE_TAG (the compose default is only the fallback), and "
        "docker-compose.offline.yml keeps literal ':latest' because "
        "scripts/build-offline-package.sh docker-saves and re-tags images under the same names "
        "and rewrites those lines to OT_IMAGE_TAG in the packaged copy.",
    ),
    (
        r"opentranscribe-[a-z-]+:(bench|latest)",
        "Local build tags (docker-compose.bench*.yml, docker-compose.benchmark.yml, and the dev "
        "override's OT_DEV_IMAGE_TAG default): built from this checkout, never pulled.",
    ),
    (
        r"opentranscribe-backend-test:latest",
        "Local build tag of the benchmark test image (docker-compose.benchmark.yml).",
    ),
]

#: file name -> {resolved ref -> reason}. Files in release-manifest.txt ship to self-hosters
#: and cannot be re-pinned without a release.
ALLOWLIST: dict[str, dict[str, str]] = {}


def resolve(ref: str) -> str:
    """Substitute every ``${VAR:-default}`` with its default, innermost first."""
    prev = None
    while prev != ref:
        prev = ref
        ref = _VAR_DEFAULT.sub(r"\1", ref)
    return ref


def tag_of(ref: str) -> str | None:
    """Return the tag of a resolved ref, or ``@sha256:...`` digest marker, or None if bare."""
    if "@sha256:" in ref:
        return "@digest"
    last = ref.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else None


def is_pinned(ref: str) -> bool:
    """A ref is pinned by a digest, or by a non-floating tag containing a version digit."""
    tag = tag_of(ref)
    if tag is None:
        return False
    if tag == "@digest":
        return True
    return tag not in FLOATING_TAGS and any(ch.isdigit() for ch in tag)


def exemption(ref: str) -> str | None:
    for pattern, reason in EXEMPT_CLASSES:
        if re.fullmatch(pattern, ref):
            return reason
    return None


def collect() -> list[tuple[str, int, str, str]]:
    """Return (file, line number, raw ref, resolved ref) for every ``image:`` line."""
    found = []
    for path in sorted(REPO_ROOT.glob("docker-compose*.yml")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            m = _IMAGE_LINE.match(line)
            if m:
                found.append((path.name, n, m.group(1), resolve(m.group(1))))
    return found


def violations() -> list[str]:
    bad = []
    for name, n, raw, ref in collect():
        if is_pinned(ref) or exemption(ref) or ref in ALLOWLIST.get(name, {}):
            continue
        bad.append(f"{name}:{n}: {raw} (resolves to {ref})")
    return bad


def test_third_party_images_are_pinned() -> None:
    bad = violations()
    assert not bad, (
        "Unpinned third-party images (pin to a version tag or @sha256 digest):\n" + "\n".join(bad)
    )


def test_scan_finds_images() -> None:
    # Guards against the regex silently matching nothing (a vacuous pass).
    refs = {ref for _, _, _, ref in collect()}
    assert len(refs) > 10
    assert "postgres:17.5-alpine" in refs


def test_exempt_classes_and_allowlist_are_not_stale() -> None:
    refs = [(name, ref) for name, _, _, ref in collect()]
    for pattern, _ in EXEMPT_CLASSES:
        assert any(re.fullmatch(pattern, ref) for _, ref in refs), f"unused exemption {pattern}"
    for name, entries in ALLOWLIST.items():
        for ref in entries:
            assert (name, ref) in refs, f"stale allowlist entry {name}: {ref}"


@pytest.mark.parametrize(
    "raw",
    [
        "smallstep/step-ca:latest",
        "lldap/lldap:stable",
        "docker.io/library/redis:alpine",
        "ollama/ollama:${LLM_TEST_OLLAMA_TAG:-latest}",
        "redis",
        "${BACKEND_IMAGE:-someone/else-backend:${TAG:-latest}}",
    ],
)
def test_detector_flags_floating_refs(raw: str) -> None:
    ref = resolve(raw)
    assert not is_pinned(ref)
    assert exemption(ref) is None


@pytest.mark.parametrize(
    "raw",
    [
        "redis:8.2.2-alpine3.22",
        "vllm/vllm-openai:${LLM_TEST_VLLM_TAG:-gemma4-cu130}",
        "x/y@sha256:" + "a" * 64,
        "registry:5000/x/y:1.2.3",
    ],
)
def test_detector_accepts_pinned_refs(raw: str) -> None:
    assert is_pinned(resolve(raw))
