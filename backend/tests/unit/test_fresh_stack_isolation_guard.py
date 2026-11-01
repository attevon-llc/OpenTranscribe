"""``assert_fresh_stack_isolated`` refuses an ``otfresh-*`` project attached to live data.

Observed 2026-10-29: a release ``test`` run recreated a fresh stack's services under the stock
names with the NAS overlay, so the "isolated" project mounted the live Postgres directory. The
real function is extracted from the library and driven with a stub ``docker`` on PATH.
"""

from __future__ import annotations

import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "compose-project.sh"

_DOCKER = """#!/usr/bin/env bash
case "$1" in
  ps)
    if [[ "$*" == *"service=postgres"* ]]; then printf '%b' "$OT_PG_NAME"; else printf '%b' "$OT_ALL_NAMES"; fi ;;
  inspect) printf '%b' "$OT_MOUNTS" ;;
esac
"""


def _run(
    tmp_path: Path, project: str, names: str, pg: str, mounts: str
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    script = textwrap.dedent(
        f"""
        set -uo pipefail
        export COMPOSE_PROJECT_NAME='{project}'
        eval "$(sed -n '/^compose_project_name()/,/^}}/p' '{LIB}')"
        eval "$(sed -n '/^assert_fresh_stack_isolated()/,/^}}/p' '{LIB}')"
        assert_fresh_stack_isolated
        echo PASSED
        """
    )
    return subprocess.run(  # nosec B603 B607 - fixed argv, stub docker
        ["bash", "-c", script],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "OT_ALL_NAMES": names,
            "OT_PG_NAME": pg,
            "OT_MOUNTS": mounts,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


NAMED_VOLUME = "volume /var/lib/postgresql/data/ /var/lib/docker/volumes/x/_data\\n"
LIVE_BIND = "bind /var/lib/postgresql/data/ /mnt/nvm/opentranscribe/pg\\n"


@pytest.mark.unit
def test_a_properly_isolated_fresh_stack_passes(tmp_path):
    result = _run(
        tmp_path,
        "otfresh-master",
        "otfresh-master-backend\\notfresh-master-postgres\\n",
        "otfresh-master-postgres\\n",
        NAMED_VOLUME,
    )
    assert result.returncode == 0, result.stderr
    assert "PASSED" in result.stdout


@pytest.mark.unit
def test_a_container_with_a_stock_name_in_a_fresh_project_is_refused(tmp_path):
    result = _run(
        tmp_path,
        "otfresh-master",
        "opentranscribe-backend\\notfresh-master-postgres\\n",
        "otfresh-master-postgres\\n",
        NAMED_VOLUME,
    )
    assert result.returncode == 3
    assert "opentranscribe-backend" in result.stderr
    assert "PASSED" not in result.stdout


@pytest.mark.unit
def test_a_postgres_bind_mounting_a_host_path_is_refused(tmp_path):
    result = _run(
        tmp_path,
        "otfresh-master",
        "otfresh-master-postgres\\n",
        "otfresh-master-postgres\\n",
        LIVE_BIND,
    )
    assert result.returncode == 3
    assert "live data" in result.stderr


@pytest.mark.unit
def test_the_stock_stack_is_never_judged(tmp_path):
    result = _run(
        tmp_path,
        "transcribe-app",
        "opentranscribe-backend\\n",
        "opentranscribe-postgres\\n",
        LIVE_BIND,
    )
    assert result.returncode == 0, result.stderr
    assert "PASSED" in result.stdout
