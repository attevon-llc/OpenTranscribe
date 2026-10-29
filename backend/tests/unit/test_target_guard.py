"""scripts/lib/target_guard.py and the two shell scripts that call it refuse the live stack.

Every spelling of "this machine's own published ports" must be refused unless the operator
passes the explicit override, and the shell scripts must refuse before running curl or docker.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
GUARD = ROOT / "scripts" / "lib" / "target_guard.py"
SMOKE = ROOT / "scripts" / "public-smoke.sh"
ZAP = ROOT / "scripts" / "zap-baseline.sh"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("target_guard", GUARD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


tg = _load()

NAMES = {
    "localtest.me": ["127.0.0.1"],
    "myhost.lan": ["10.10.10.20"],
    "dual.example": ["93.184.216.34", "127.0.0.1"],
    "public.example": ["93.184.216.34"],
    "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
}
LOCAL = {"10.10.10.20"}


def _resolve(host: str, port: int) -> list[str]:
    if host in NAMES:
        return NAMES[host]
    raise OSError(f"no such host {host}")


def _check(url: str, allow: bool = False) -> bool:
    """True when the guard let the URL through; Refused/Unusable propagate."""
    tg.assert_not_live(url, allow_live=allow, resolve=_resolve, local_addresses=lambda: LOCAL)
    return True


REFUSED = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://localhost.:5173",
    "http://LOCALHOST:5173",
    "http://good@localhost:5173",
    "http://user:pw@127.0.0.1:5174/path",
    "http://0.0.0.0:5173",
    "http://[::]:5173",
    "http://[::1]:5173",
    "http://[::ffff:127.0.0.1]:5173",
    "http://2130706433:5174",
    "http://0x7f000001:5174",
    "http://0177.0.0.1:5174",
    "http://127.1:5174",
    "http://localhost:5173?x",
    "http://localhost:5173#f",
    "http://localhost:05173",
    "http://localtest.me:5173",
    "http://dual.example:5174",
    "http://myhost.lan:5174",
    "http://10.10.10.20:5174",
    "http://169.254.1.1:5173",
    "http://[fe80::1]:5173",
    "http://127.0.0.1:5182",
    "http://127.0.0.1:5190",
    "https://localhost",
    "http://localhost",
    "https://localhost:443/x",
    "http://127.0.0.1:80",
]

ALLOWED = [
    "http://127.0.0.1:5473",
    "http://127.0.0.1:5373",
    "https://public.example",
    "https://public.example:5173",
    "https://v6.example:443",
    "http://127.0.0.1:8443",
]


@pytest.mark.parametrize("url", REFUSED)
def test_live_stack_spellings_are_refused(url: str) -> None:
    with pytest.raises(tg.Refused):
        _check(url)


@pytest.mark.parametrize("url", REFUSED)
def test_override_allows_every_refused_spelling(url: str) -> None:
    assert _check(url, allow=True)


@pytest.mark.parametrize("url", ALLOWED)
def test_fresh_and_public_targets_are_allowed(url: str) -> None:
    assert _check(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://localhost",
        "http://",
        "http://127.0.0.1:abc",
        "http://127.0.0.1:99999",
        "http://nope.invalid:5473",
    ],
)
def test_unparseable_or_unresolvable_targets_are_unusable_not_allowed(url: str) -> None:
    with pytest.raises(tg.Unusable):
        _check(url)


# --- the shell scripts, with curl/docker stubbed so nothing real is contacted ----------------------


@pytest.fixture
def stubs(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("curl", "docker"):
        stub = bindir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return env, log


def _sh(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


SHELL_REFUSED = [
    "http://0.0.0.0:5173",
    "http://localhost.:5173",
    "http://LOCALHOST:5173",
    "http://good@localhost:5173",
    "http://[::ffff:127.0.0.1]:5173",
    "http://2130706433:5174",
    "http://localhost:5173?x",
    "http://localhost:05173",
]


@pytest.mark.parametrize("url", SHELL_REFUSED)
def test_smoke_refuses_before_running_anything(
    url: str, stubs: tuple[dict[str, str], Path]
) -> None:
    env, log = stubs
    result = _sh(SMOKE, env, url, "--allow-http")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("url", SHELL_REFUSED)
def test_zap_refuses_before_running_docker(
    url: str, stubs: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    env, log = stubs
    result = _sh(ZAP, env, url, str(tmp_path / "reports"))
    assert result.returncode == 64, result.stdout + result.stderr
    assert "refusing" in result.stderr
    assert not log.exists()
    assert not (tmp_path / "reports").exists()


def test_override_flag_lets_zap_past_the_guard(
    stubs: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    env, log = stubs
    result = _sh(ZAP, env, "http://localhost:5173", str(tmp_path / "r"), "--allow-live-dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docker run" in log.read_text()


def test_zap_only_touches_a_fresh_subdirectory(
    stubs: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    env, log = stubs
    parent = tmp_path / "reports"
    parent.mkdir(mode=0o700)
    (parent / "rules.tsv").write_text("precious\n")
    result = _sh(ZAP, env, "http://127.0.0.1:5473", str(parent))
    assert result.returncode == 0, result.stdout + result.stderr
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert (parent / "rules.tsv").read_text() == "precious\n"
    work = [p for p in parent.iterdir() if p.is_dir()]
    assert len(work) == 1 and work[0].name.startswith("zap.")
    run_line = next(line for line in log.read_text().splitlines() if line.startswith("docker run"))
    assert f"{work[0]}:/zap/wrk" in run_line
    assert f"{parent}:/zap/wrk" not in run_line
    copied = work[0] / "rules.tsv"
    assert copied.read_text() == (ROOT / ".zap" / "rules.tsv").read_text()
    assert not os.access(copied, os.W_OK) or os.geteuid() == 0


def test_smoke_flags_a_dead_target_as_unreachable_not_findings(
    stubs: tuple[dict[str, str], Path],
) -> None:
    env, _ = stubs
    result = _sh(SMOKE, env, "http://127.0.0.1:9", "--allow-http")
    assert result.returncode == 2, result.stdout + result.stderr
