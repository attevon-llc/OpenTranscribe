"""The locally-built Keycloak test image must carry a tag that CHANGES when its build does.

``docker-compose.keycloak.yml`` declares both ``build:`` and ``image:``. That combination means
"build only if this tag is absent locally" — compose never looks at the Dockerfile to decide.
Tagged with the upstream version alone (``opentranscribe-keycloak-test:26.7.3``), an edit to
``docker/keycloak/Dockerfile`` was therefore never rebuilt on any machine that had once built
the image; the stale one just kept starting.

That is worse here than ordinary staleness, because the container starts with
``kc.sh start --optimized``. ``--optimized`` asserts the augmentation was already done at build
time, and Keycloak REFUSES to boot when the build-time options baked into the image disagree
with the ones present at run time — it will not silently re-augment. So the failure of a stale
image is a Keycloak that never becomes healthy, surfacing as an opaque ``up --wait`` timeout
against the 120 s ``start_period``. The natural readings of that symptom ("the healthcheck
budget is too tight again", from a file whose history is exactly that) are both wrong, and the
natural fix — raise the budget — cannot work.

The mechanism: the tag carries a truncated sha256 of the **build context**. Change the context
and the tag names an image that does not exist locally, so compose builds it. This module is
what makes that a gate rather than a convention: it recomputes the digest and fails when the
two drift. A comment asking people to bump a tag is not a mechanism.

Static by construction — no docker, no Keycloak, no network. It belongs in the fast unit suite
precisely because the runtime symptom is a ten-minute timeout in a different script.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.keycloak.yml"
CONTEXT = REPO_ROOT / "docker" / "keycloak"

#: How many hex characters of the digest the tag carries. 8 is 4 bytes: collisions are a
#: birthday problem over the handful of revisions this file ever has, not over a keyspace.
_DIGEST_CHARS = 8

pytestmark = pytest.mark.skipif(
    not COMPOSE.is_file() or not CONTEXT.is_dir(),
    reason="docker-compose.keycloak.yml / docker/keycloak not present in this checkout",
)


def _context_digest(context: Path, chars: int = _DIGEST_CHARS) -> str:
    """A stable digest of every file in a docker build context.

    Names are hashed alongside contents, so a rename is a change; files are visited in sorted
    order, so the digest does not depend on directory iteration order. The whole context is
    covered rather than just the Dockerfile, because anything a future ``COPY`` pulls in is
    baked into the image just as firmly.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in context.rglob("*") if p.is_file()):
        digest.update(path.relative_to(context).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:chars]


def _keycloak_image_ref() -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"^\s*image:\s*(opentranscribe-keycloak-test:\S+)\s*$", text, re.M)
    assert match, (
        "docker-compose.keycloak.yml no longer declares an `image:` for the locally built "
        "Keycloak. Without one, compose names the image after the project and this entire "
        "rebuild contract disappears."
    )
    return match.group(1)


def _dockerfile_from_versions() -> set[str]:
    dockerfile = (CONTEXT / "Dockerfile").read_text(encoding="utf-8")
    return set(
        re.findall(
            r"^FROM\s+quay\.io/keycloak/keycloak:(\S+?)(?:\s+AS\s+\S+)?\s*$",
            dockerfile,
            re.M | re.I,
        )
    )


def test_the_image_tag_carries_the_build_contexts_digest():
    """The whole mechanism. If this fails, bump the suffix — do not delete the check."""
    tag = _keycloak_image_ref().split(":", 1)[1]
    expected = _context_digest(CONTEXT)
    assert tag.endswith(f"-{expected}"), (
        f"the Keycloak test image is tagged {tag!r}, but docker/keycloak/ now hashes to "
        f"{expected!r}. Compose builds only when the tag is ABSENT locally, so as written an "
        "edited Dockerfile is never rebuilt — and with `start --optimized` a stale image "
        "does not degrade, it refuses to boot, as an unexplained `up --wait` timeout.\n"
        f"Fix: set `image: opentranscribe-keycloak-test:<version>-{expected}` in "
        "docker-compose.keycloak.yml."
    )


def test_the_image_tag_still_names_the_pinned_upstream_version():
    """The digest must not cost us the readable pin (issue #492)."""
    versions = _dockerfile_from_versions()
    assert versions, "docker/keycloak/Dockerfile has no `FROM quay.io/keycloak/keycloak:<v>`"
    assert "latest" not in versions, (
        "docker/keycloak/Dockerfile is back on a floating tag — the gate's auth phase would "
        "run against whatever Keycloak shipped that morning (issue #492)"
    )
    assert len(versions) == 1, (
        f"the builder and runtime stages disagree on the Keycloak version: {sorted(versions)}. "
        "`--optimized` requires the augmented distribution and the runtime to be the same "
        "build."
    )
    version = versions.pop()
    tag = _keycloak_image_ref().split(":", 1)[1]
    assert tag.startswith(f"{version}-"), (
        f"the image tag {tag!r} does not lead with the Dockerfile's pinned version "
        f"{version!r}. The digest tells you the build changed; only the version tells you "
        "WHICH Keycloak you are testing against — and a silent downgrade against a "
        "`keycloak_data` volume written by a newer release is how this file broke before."
    )


def test_the_compose_file_builds_the_context_this_module_hashes():
    """A digest of the wrong directory would be a green check over an unhashed build."""
    text = COMPOSE.read_text(encoding="utf-8")
    declared = re.search(r"^\s*context:\s*(\S+)\s*$", text, re.M)
    assert declared, "the keycloak service no longer declares a build `context:`"
    resolved = (REPO_ROOT / declared.group(1)).resolve()
    assert resolved == CONTEXT.resolve(), (
        f"compose builds {resolved}, this module hashes {CONTEXT.resolve()} — the tag would "
        "track a directory nobody builds from."
    )


def test_the_bootstrap_admin_uses_the_variables_keycloak_26_actually_reads():
    """`KEYCLOAK_ADMIN` was renamed in Keycloak 26 and is inert on an ALREADY-bootstrapped volume.

    That is the trap: on this host the `keycloak_data` volume already holds an admin, so the
    old names are never consulted and nothing looks wrong. On a `--fresh` stack, after
    `fresh-destroy`, or on a new machine, the volume is empty, no admin is created, and
    admin/admin is rejected — which reads as "the credentials in CLAUDE.md are wrong" rather
    than as a compose defect. A test is the only thing that can see this, because the working
    case and the broken case differ only by the state of a volume.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    for name in ("KC_BOOTSTRAP_ADMIN_USERNAME", "KC_BOOTSTRAP_ADMIN_PASSWORD"):
        assert re.search(rf"^\s*{name}\s*:", text, re.M), (
            f"{name} is not set on the keycloak service. Keycloak 26 bootstraps its temporary "
            "admin from KC_BOOTSTRAP_ADMIN_*; without them a fresh volume gets no admin at all."
        )
    for legacy in ("KEYCLOAK_ADMIN", "KEYCLOAK_ADMIN_PASSWORD"):
        assert not re.search(rf"^\s*{legacy}\s*:", text, re.M), (
            f"{legacy} is set as a container variable again. It was deprecated in Keycloak "
            "26.0 and replaced by KC_BOOTSTRAP_ADMIN_*; keeping both is two paths doing one "
            "job, and the dead one is the one that looks right."
        )


# ---------------------------------------------------------------------------------------
# Guard the guard. A digest function that ignored its input would pass everything above.
# ---------------------------------------------------------------------------------------


def test_an_edited_dockerfile_produces_a_different_digest(tmp_path: Path):
    context = tmp_path / "ctx"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text("FROM quay.io/keycloak/keycloak:26.7.3\nENV KC_DB=dev-file\n")
    before = _context_digest(context)

    dockerfile.write_text("FROM quay.io/keycloak/keycloak:26.7.3\nENV KC_DB=postgres\n")
    assert _context_digest(context) != before, (
        "a changed build-time option left the digest unmoved — the tag would not change, "
        "compose would not rebuild, and `--optimized` would refuse to start against the "
        "mismatch this exact edit creates"
    )


def test_the_digest_covers_the_whole_context_not_only_the_dockerfile(tmp_path: Path):
    """A `COPY`-able sibling is as baked-in as the Dockerfile itself."""
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM quay.io/keycloak/keycloak:26.7.3\n")
    before = _context_digest(context)

    (context / "realm-export.json").write_text('{"realm": "ot"}\n')
    after_add = _context_digest(context)
    assert after_add != before, "adding a file to the build context left the digest unmoved"

    (context / "realm-export.json").write_text('{"realm": "other"}\n')
    assert _context_digest(context) != after_add, (
        "editing a non-Dockerfile file in the context left the digest unmoved"
    )


def test_renaming_a_context_file_changes_the_digest(tmp_path: Path):
    """Contents alone are not enough: `COPY realm.json` cares which name holds them."""
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM quay.io/keycloak/keycloak:26.7.3\n")
    (context / "a.json").write_text("{}\n")
    before = _context_digest(context)

    (context / "a.json").rename(context / "b.json")
    assert _context_digest(context) != before, (
        "a rename left the digest unmoved — the same bytes under a different name is a "
        "different image"
    )


def test_the_digest_is_stable_for_an_unchanged_context():
    """The other direction: it must not churn, or the gate becomes noise people disable."""
    assert _context_digest(CONTEXT) == _context_digest(CONTEXT)
    assert re.fullmatch(r"[0-9a-f]{8}", _context_digest(CONTEXT))
