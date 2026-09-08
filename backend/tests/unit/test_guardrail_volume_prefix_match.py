"""Cleanup's volume prefix guard must match the names compose actually creates.

``gr_cleanup`` only removes a volume whose name matches a test prefix — a good rule, since it
runs ``docker volume rm``. The guard accepted ``"${TEST_PROJECT_NAME//-/_}"*`` (project name
with hyphens turned into underscores) and ``ot_reltest_*``.

Compose prefixes a volume with the project name **verbatim**; hyphens are legal in a project
name and are not substituted. So project ``ot-reltest-lite`` produces
``ot-reltest-lite_postgres_data``, which matched neither pattern::

    ⚠ refusing to remove volume 'ot-reltest-lite_postgres_data' — name does not match test prefix

The guard written to stop the WRONG volume being deleted refused every RIGHT one. The stale
Postgres then survived into the next lite-mode run, whose backend died at startup with

    CRITICAL: Database migration failed — aborting startup:
    FATAL:  password authentication failed for user "postgres"

— exactly the failure ``gr_check_stale_stock_volumes`` warns about, one scenario over. The
lite scenario passed once (fresh volume) and failed the next run, which reads as flakiness
and is not.

⚠️ **These tests drive the real function against real volumes.** A static check would have
been written against the same wrong assumption about the name — the whole defect was a belief
about what compose produces, so the test has to observe it rather than restate it.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARDRAILS = REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"

pytestmark = pytest.mark.skipif(
    not GUARDRAILS.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/lib/guardrails.sh or bash is not present in this checkout",
)

needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available on this host"
)


def _prefix_case_accepts(project: str, volume: str) -> bool:
    """Drive the REAL `case` arm out of gr_cleanup against one volume name."""
    source = GUARDRAILS.read_text(encoding="utf-8")
    marker = '"${TEST_PROJECT_NAME}"*|'
    assert marker in source, (
        "the volume prefix case arm has changed shape; re-point this guard rather than "
        "letting it silently test nothing"
    )
    arm = source[source.index(marker) : source.index(")", source.index(marker))]

    harness = textwrap.dedent(f"""
        TEST_PROJECT_NAME={project!r}
        vol={volume!r}
        case "$vol" in
            {arm}) echo ACCEPT ;;
            *) echo REFUSE ;;
        esac
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60, check=False
    )
    return "ACCEPT" in result.stdout


@pytest.mark.parametrize(
    ("project", "volume"),
    [
        # The exact name that was refused, and the reason the lite scenario broke.
        ("ot-reltest-lite", "ot-reltest-lite_postgres_data"),
        ("ot-reltest-lite", "ot-reltest-lite_minio_data"),
        ("ot-reltest-fresh", "ot-reltest-fresh_opensearch_data"),
        # The underscored form must keep working — it was the only one accepted before.
        ("ot-reltest-lite", "ot_reltest_lite_postgres_data"),
    ],
)
def test_a_volume_this_run_created_is_accepted(project: str, volume: str):
    assert _prefix_case_accepts(project, volume), (
        f"cleanup refuses {volume!r} for project {project!r}. compose prefixes volumes with "
        "the project name VERBATIM (hyphens are legal and not substituted), so this is a "
        "volume the run owns — refusing it leaves a stale database whose credentials break "
        "the NEXT run's backend."
    )


@pytest.mark.parametrize(
    "volume",
    [
        "opentranscribe_postgres_data",  # the LIVE deployment's data
        "some-other-project_postgres_data",
        "postgres_data",
    ],
)
def test_a_volume_outside_the_test_namespace_is_still_refused(volume: str):
    """The must-refuse control. Widening the prefix must not have widened it to everything."""
    assert not _prefix_case_accepts("ot-reltest-lite", volume), (
        f"cleanup would now remove {volume!r}, which is outside the ot-reltest namespace. "
        "The prefix guard exists because this code runs `docker volume rm`."
    )


def test_cleanup_also_selects_volumes_by_the_test_compose_project():
    """Same ordering gap as the containers: a late-added overlay's volume has no label."""
    source = GUARDRAILS.read_text(encoding="utf-8")
    assert (
        'docker volume ls -q --filter "label=com.docker.compose.project=$TEST_PROJECT_NAME"'
        in source
    ), (
        "gr_cleanup selects volumes only by the release-test label. A volume declared by an "
        "overlay the installer added after the labelling step carries no such label and "
        "survives — the prefix guard never even gets to see it."
    )


@needs_docker
def test_compose_really_does_keep_the_hyphens():
    """Prove the premise instead of restating it.

    The entire defect was a belief about how compose names a volume. If that ever changes,
    this module's parametrisation is wrong and should fail loudly rather than pass.
    """
    project = f"ot-reltest-probe-{uuid.uuid4().hex[:8]}"
    compose = textwrap.dedent("""
        services:
          nothing:
            image: alpine:3.20
            command: ["true"]
            volumes:
              - probe_data:/probe
        volumes:
          probe_data:
    """)
    tmp = Path(subprocess.run(["mktemp", "-d"], capture_output=True, text=True).stdout.strip())
    (tmp / "docker-compose.yml").write_text(compose, encoding="utf-8")
    try:
        subprocess.run(
            ["docker", "compose", "-p", project, "-f", str(tmp / "docker-compose.yml"), "create"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        listing = subprocess.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
        created = [v for v in listing.splitlines() if project in v]
        assert created, f"no volume was created for project {project}; cannot verify the premise"
        assert created[0] == f"{project}_probe_data", (
            f"compose named the volume {created[0]!r}, not {project}_probe_data — the "
            "verbatim-project-name premise this module is built on has changed"
        )
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(tmp / "docker-compose.yml"),
                "down",
                "-v",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
