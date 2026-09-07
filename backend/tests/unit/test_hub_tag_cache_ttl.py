"""The Docker Hub tag memo must live somewhere stable AND expire.

``ver_hub_has`` (``scripts/release-tests/lib/versions.sh``) memoizes
``docker manifest inspect`` because it counts against the anonymous 100-pulls-per-6h limit and
previous-version detection probes several tags. The memo used to live at
``${TEST_ROOT:-${TMPDIR:-/tmp}}/.hub-tags``, which was wrong in both directions at once:

* ``TEST_ROOT`` is a per-run TIMESTAMPED directory, so a rehearsal started from a clean root
  re-probed every tag — the memo saved nothing in the case it was written for;
* with ``TEST_ROOT`` unset (``release.sh status``, ``90-promote.sh``) it fell back to a bare
  ``/tmp/.hub-tags`` that never expired, so a cached **"no"** for a tag published five minutes
  later stayed "no" forever. That is the dangerous direction: it makes a published release
  invisible to the tooling that decides what to rehearse against.

A stable directory alone fixes the first and makes the second permanent; a TTL alone leaves the
first. Both, or neither.

Driven by SOURCING the real function against a fake ``docker`` on ``PATH``, because a grep for
``OT_HUB_CACHE_TTL_S`` would pass against a version that defines it and never reads it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_SH = REPO_ROOT / "scripts" / "release-tests" / "lib" / "versions.sh"

pytestmark = pytest.mark.skipif(
    not VERSIONS_SH.is_file() or shutil.which("bash") is None,
    reason="versions.sh or bash is not present in this checkout",
)


def _probe(tmp_path: Path, body: str) -> tuple[str, int]:
    """Source versions.sh with a recording fake ``docker`` and run ``body``.

    The fake answers "published" for v1.0.0 and "not published" for anything else, and appends
    one line per invocation, so a caller can count how many real probes happened.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    (bindir / "docker").write_text(
        f'#!/bin/bash\necho "$*" >> "{log}"\n[[ "$*" == *v1.0.0* ]] && exit 0\nexit 1\n',
        encoding="utf-8",
    )
    (bindir / "docker").chmod(0o755)

    script = textwrap.dedent(f"""
        set -uo pipefail
        PATH="{bindir}:$PATH"
        REPO_ROOT={REPO_ROOT}
        DOCKERHUB_USERNAME=davidamacey
        gr_die() {{ echo "die: $*" >&2; return 1; }}
        export OT_HUB_CACHE_DIR="{tmp_path}/cache"
        source "{VERSIONS_SH}"
        probes() {{ [[ -f "{log}" ]] && grep -c . "{log}" || echo 0; }}
    """) + textwrap.dedent(body)
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    return proc.stdout.strip(), proc.returncode


def test_repeated_questions_hit_the_memo_not_docker_hub(tmp_path: Path):
    """Four lookups over two tags must issue two probes. The memo's whole purpose."""
    out, _ = _probe(
        tmp_path,
        """
        for i in 1 2; do
            ver_hub_has backend v1.0.0 >/dev/null || true
            ver_hub_has backend v9.9.9 >/dev/null || true
        done
        echo "PROBES:$(probes)"
        """,
    )
    assert "PROBES:2" in out, f"expected 2 docker probes for 4 lookups over 2 tags, got: {out!r}"


def test_the_memo_still_returns_the_right_answers(tmp_path: Path):
    """Control: "never answer yes" would satisfy the count test above."""
    out, _ = _probe(
        tmp_path,
        """
        ver_hub_has backend v1.0.0 && echo "PUBLISHED:yes" || echo "PUBLISHED:no"
        ver_hub_has backend v9.9.9 && echo "MISSING:yes" || echo "MISSING:no"
        ver_hub_has backend v1.0.0 && echo "CACHED:yes" || echo "CACHED:no"
        """,
    )
    assert "PUBLISHED:yes" in out and "MISSING:no" in out and "CACHED:yes" in out, out


def test_an_expired_entry_is_re_probed(tmp_path: Path):
    """A cached "no" must not outlive the publish it was recorded before.

    Expiry is forced by rewriting the stamps to epoch 1 rather than by sleeping: the TTL is
    6 hours and a test may not wait for it.
    """
    out, _ = _probe(
        tmp_path,
        """
        ver_hub_has backend v9.9.9 >/dev/null || true
        cache="$(_ver_hub_cache)"
        awk '{print $1, $2, 1}' "$cache" > "$cache.tmp" && mv "$cache.tmp" "$cache"
        ver_hub_has backend v9.9.9 >/dev/null || true
        echo "PROBES:$(probes)"
        """,
    )
    assert "PROBES:2" in out, (
        f"an expired entry was served from the memo instead of re-probed: {out!r}"
    )


def test_a_pre_ttl_entry_without_a_timestamp_is_treated_as_expired(tmp_path: Path):
    """The never-expiring /tmp entries this change retires must not be honoured.

    They have two fields, not three. Reading a missing stamp as "fresh" would preserve exactly
    the permanent stale "no" the TTL exists to end.
    """
    out, _ = _probe(
        tmp_path,
        """
        cache="$(_ver_hub_cache)"
        mkdir -p "$(dirname "$cache")"
        printf 'davidamacey/opentranscribe-backend:v1.0.0 no\\n' > "$cache"
        ver_hub_has backend v1.0.0 && echo "ANSWER:yes" || echo "ANSWER:no"
        echo "PROBES:$(probes)"
        """,
    )
    assert "PROBES:1" in out, f"a stampless entry was honoured rather than re-probed: {out!r}"
    assert "ANSWER:yes" in out, (
        f"the re-probe found the tag published but the stale 'no' still won: {out!r}"
    )


def test_the_cache_directory_does_not_depend_on_the_per_run_test_root(tmp_path: Path):
    """TEST_ROOT is per-run and timestamped; a memo keyed on it memoizes nothing."""
    source = VERSIONS_SH.read_text(encoding="utf-8")
    start = source.index("_ver_hub_cache() {")
    body = source[start : source.index("\n}", start)]
    assert "TEST_ROOT" not in body, (
        "_ver_hub_cache reads TEST_ROOT again. That is a per-run timestamped directory, so "
        "every rehearsal re-probes every tag against a 100-pulls-per-6h budget — and when it "
        "is unset the fallback was a /tmp file that never expired."
    )
    assert "OT_HUB_CACHE_DIR" in body, "the override OT_HUB_CACHE_DIR is gone"


@pytest.mark.skipif(os.environ.get("CI") == "true", reason="reads the checked-in script only")
def test_the_ttl_is_configurable_and_has_a_default():
    source = VERSIONS_SH.read_text(encoding="utf-8")
    assert "OT_HUB_CACHE_TTL_S" in source, "the TTL is gone — a cached 'no' would be permanent"
