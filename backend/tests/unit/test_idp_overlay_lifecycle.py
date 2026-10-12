"""The e2e LDAP/Keycloak fixtures give back exactly what they took (#1222).

``test_ldap_oidc.py`` used to start the IdP overlays and leave them running "on purpose".
The decision (see ``docs/design/v060_test_hygiene_audit.md``): leave an overlay alone when the
stack already had it; take it down when the test started it. A stub ``docker`` on PATH records
what the helper asks of it, so no daemon is touched.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

_MODULE = Path(__file__).resolve().parents[1] / "e2e" / "idp_overlay.py"
_spec = importlib.util.spec_from_file_location("idp_overlay_under_test", _MODULE)
assert _spec is not None and _spec.loader is not None, f"cannot load {_MODULE}"
_overlay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _overlay  # @dataclass resolves its module through sys.modules
_spec.loader.exec_module(_overlay)
start_overlay = _overlay.start_overlay
stop_overlay = _overlay.stop_overlay

LDAP = "docker-compose.ldap-test.yml"


@pytest.fixture
def docker_calls(tmp_path, monkeypatch):
    """A stub ``docker`` that logs argv; ``ps -a -q`` answers from ``$STUB_PS_OUTPUT``."""
    log = tmp_path / "calls.log"
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir()
    stub.write_text(
        '#!/bin/bash\necho "$*" >> "$STUB_LOG"\n'
        '[[ "$*" == *" ps -a -q"* ]] && printf "%s" "${STUB_PS_OUTPUT:-}"\nexit 0\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{stub.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))

    def calls() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    return calls


def _start(tmp_path, *, port_open: bool, services=()):
    return start_overlay(
        LDAP, services, cwd=tmp_path, port=3890, is_open=lambda _host, _port: port_open
    )


def test_an_overlay_the_stack_already_runs_is_never_touched(tmp_path, docker_calls):
    handle = _start(tmp_path, port_open=True)
    stop_overlay(handle)
    assert handle.started_by_test is False
    assert docker_calls() == [], "no docker command may run for an overlay the stack owns"


def test_an_overlay_the_test_created_is_taken_down(tmp_path, docker_calls):
    handle = _start(tmp_path, port_open=False)
    assert handle.started_by_test and not handle.container_existed
    stop_overlay(handle)
    calls = docker_calls()
    assert f"compose -f {LDAP} up -d" in calls
    assert f"compose -f {LDAP} down" in calls
    assert not any(" -v" in c for c in calls), "a named volume is not provably ours"


def test_a_stopped_container_that_predates_the_test_is_stopped_not_removed(
    tmp_path, docker_calls, monkeypatch
):
    monkeypatch.setenv("STUB_PS_OUTPUT", "abc123def456\n")
    handle = _start(tmp_path, port_open=False, services=("keycloak",))
    assert handle.started_by_test and handle.container_existed
    stop_overlay(handle)
    calls = docker_calls()
    assert f"compose -f {LDAP} stop keycloak" in calls
    assert not any(c.endswith(" down") or " rm" in c for c in calls), (
        "a container the test did not create must not be removed"
    )
