"""A running install sends nothing to third parties by default — telemetry and update checks.

Every switch asserted here was verified against the shipped dependency, not assumed:

* ``PYANNOTE_METRICS_ENABLED`` — the pinned pyannote.audio fork ships
  ``telemetry/config.yaml`` with ``metrics_enabled: true`` and an OTLP exporter to
  ``https://otel.pyannote.ai/v1/metrics``. Probed in the built image with HTTP intercepted:
  unset (the old default) POSTed to otel.pyannote.ai; ``false`` made zero requests, including
  on flush/shutdown. If unset at import, the module writes ``"true"`` into ``os.environ``
  itself, so the code-level default must run BEFORE pyannote is imported.
* ``HF_HUB_DISABLE_TELEMETRY`` / ``DO_NOT_TRACK`` — huggingface_hub reads them into module
  constants at import (``send_telemetry`` and the User-Agent version fingerprint).
* ``DENO_NO_UPDATE_CHECK`` — the deno binary yt-dlp spawns fetched its latest release from
  dl.deno.land (observed: ``$DENO_DIR/latest.txt`` written) unless set.
* ``MINIO_UPDATE=off`` — MinIO resolved ``dl.min.io`` on every server start (DNS-logged)
  unless set.

The compose checks are static per-file (no docker needed) plus a real
``docker compose config`` merge for the deployment shapes ``opentr.sh`` assembles.
"""

from __future__ import annotations

import ast
import functools
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

#: Env every backend-image container (API, every celery worker, beat, flower, sidecars)
#: must get, with the value that turns the third-party call OFF.
BACKEND_TELEMETRY_OFF = {
    "PYANNOTE_METRICS_ENABLED": "false",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "DENO_NO_UPDATE_CHECK": "1",
}

#: Values that would switch one of the above back ON if an overlay set them.
_ENABLING = {"true", "1", "yes", "on"}
_DISABLING_FOR = {
    "PYANNOTE_METRICS_ENABLED": {"false", "0", "no", "off"},
}

#: Third-party images with their own phone-home, keyed by service name.
THIRD_PARTY_OFF: dict[str, dict[str, str]] = {
    "minio": {"MINIO_UPDATE": "off", "MINIO_CALLHOME_ENABLE": "off"},
    "grafana": {
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        "GF_ANALYTICS_FEEDBACK_LINKS_ENABLED": "false",
        "GF_NEWS_NEWS_FEED_ENABLED": "false",
        "GF_SECURITY_DISABLE_GRAVATAR": "true",
        "GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED": "true",
    },
    "llm-test-vllm": {"VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1"},
    "authentik-server": {
        "AUTHENTIK_DISABLE_UPDATE_CHECK": "true",
        "AUTHENTIK_DISABLE_STARTUP_ANALYTICS": "true",
        "AUTHENTIK_ERROR_REPORTING__ENABLED": "false",
    },
    "authentik-worker": {
        "AUTHENTIK_DISABLE_UPDATE_CHECK": "true",
        "AUTHENTIK_DISABLE_STARTUP_ANALYTICS": "true",
        "AUTHENTIK_ERROR_REPORTING__ENABLED": "false",
    },
}

#: Backend Dockerfiles whose images run the API/workers directly. Dockerfile.test builds
#: FROM the prod image and inherits its ENV.
BACKEND_DOCKERFILES = ("Dockerfile.prod", "Dockerfile.lite", "Dockerfile.blackwell")

_INTERP = re.compile(r"^\$\{(?P<name>[A-Z0-9_]+)(?::?-(?P<default>[^}]*))?\}$")

COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))


@functools.cache
def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _env_map(service: dict) -> dict[str, str | None]:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return {k: (None if v is None else str(v)) for k, v in env.items()}
    out: dict[str, str | None] = {}
    for item in env:
        key, sep, value = str(item).partition("=")
        out[key] = value if sep else None
    return out


def _effective_default(raw: str | None) -> str | None:
    """The value a ``${VAR:-default}`` resolves to when the operator has not set VAR."""
    if raw is None:
        return None
    match = _INTERP.match(raw.strip())
    if match:
        return match.group("default")
    return raw


def _is_backend_image(service: dict) -> bool:
    image = str(service.get("image") or "")
    build = service.get("build")
    context = build if isinstance(build, str) else (build or {}).get("context", "")
    return "opentranscribe-backend" in image or str(context).rstrip("/") == "./backend"


def _backend_services() -> dict[str, Path]:
    """``{service: defining file}`` for every service that runs the backend image.

    A service defined in the base file is defined THERE (overlays only patch it, and compose
    merges ``environment`` per key), so the base file must carry the vars. A service that
    first appears in an overlay is defined by that overlay.
    """
    backend_names: set[str] = set()
    for path in COMPOSE_FILES:
        for name, service in (_load(path).get("services") or {}).items():
            if _is_backend_image(service or {}):
                backend_names.add(name)
    return {name: _defining_file(name) for name in backend_names}


def _defining_file(service_name: str) -> Path:
    """The base file if it has the service, else the overlay that gives it an image/build."""
    base_path = REPO_ROOT / "docker-compose.yml"
    if service_name in (_load(base_path).get("services") or {}):
        return base_path
    for path in COMPOSE_FILES:
        service = (_load(path).get("services") or {}).get(service_name)
        if service is not None and (service.get("image") or service.get("build")):
            return path
    raise AssertionError(f"no compose file defines an image/build for {service_name!r}")


def test_compose_files_found() -> None:
    names = {p.name for p in COMPOSE_FILES}
    assert {"docker-compose.yml", "docker-compose.prod.yml", "docker-compose.offline.yml"} <= names


def test_backend_services_detected() -> None:
    """Guard the detector itself — an empty set would make the next test vacuous."""
    services = _backend_services()
    for expected in ("backend", "celery-worker", "celery-beat", "flower", "diar-native"):
        assert expected in services, f"{expected} not detected as a backend-image service"


@pytest.mark.parametrize("service_name", sorted(_backend_services()))
def test_backend_service_defines_telemetry_off(service_name: str) -> None:
    defining = _backend_services()[service_name]
    service = (_load(defining).get("services") or {})[service_name] or {}
    env = _env_map(service)
    wrong = {
        var: env.get(var)
        for var, off in BACKEND_TELEMETRY_OFF.items()
        if _effective_default(env.get(var)) != off
    }
    assert not wrong, (
        f"{defining.name}:{service_name} must set {sorted(wrong)} to the disabled value by "
        f"default (got {wrong})"
    )


def test_no_overlay_reenables_telemetry() -> None:
    """No compose file anywhere sets one of the switches back to an enabling value."""
    checked = 0
    violations = []
    for compose_file in COMPOSE_FILES:
        for name, service in (_load(compose_file).get("services") or {}).items():
            env = _env_map(service or {})
            for var in BACKEND_TELEMETRY_OFF:
                if var not in env:
                    continue
                checked += 1
                value = (_effective_default(env[var]) or "").lower()
                if value not in _DISABLING_FOR.get(var, _ENABLING):
                    violations.append(f"{compose_file.name}:{name}: {var}={value!r}")
    assert checked >= len(BACKEND_TELEMETRY_OFF), "no telemetry switch found in any compose file"
    assert violations == [], f"compose files re-enable telemetry: {violations}"


@pytest.mark.parametrize("service_name", sorted(THIRD_PARTY_OFF))
def test_third_party_service_phone_home_off(service_name: str) -> None:
    defining = _defining_file(service_name)
    env = _env_map(_load(defining)["services"][service_name] or {})
    wrong = {
        var: env.get(var)
        for var, off in THIRD_PARTY_OFF[service_name].items()
        if (_effective_default(env.get(var)) or "").lower() != off
    }
    assert not wrong, f"{defining.name}:{service_name} must set {wrong} off"


def _dockerfile_final_stage_env(path: Path) -> dict[str, str]:
    """``ENV`` keys/values of the LAST stage (the image that ships), continuation-joined."""
    text = path.read_text().replace("\\\n", " ")
    stages = re.split(r"(?im)^FROM\s", text)
    env: dict[str, str] = {}
    for line in stages[-1].splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("ENV "):
            continue
        for token in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|\S+)", stripped[4:]):
            env[token[0]] = token[1].strip('"')
    return env


@pytest.mark.parametrize("dockerfile", BACKEND_DOCKERFILES)
def test_backend_dockerfile_env(dockerfile: str) -> None:
    env = _dockerfile_final_stage_env(BACKEND_DIR / dockerfile)
    wrong = {k: env.get(k) for k, v in BACKEND_TELEMETRY_OFF.items() if env.get(k) != v}
    assert not wrong, f"backend/{dockerfile} final stage must ENV {wrong} to the disabled value"


@pytest.mark.parametrize(
    "dockerfile", ["frontend/Dockerfile.prod", "frontend/Dockerfile.dev", "docs-site/Dockerfile"]
)
def test_npm_update_notifier_off_in_build_stages(dockerfile: str) -> None:
    text = (REPO_ROOT / dockerfile).read_text()
    npm_stages = [
        stage
        for stage in re.split(r"(?im)^FROM\s", text)[1:]
        if re.search(r"(?m)^RUN .*\bnpm\b", stage)
    ]
    assert npm_stages, f"{dockerfile}: expected at least one stage that runs npm"
    missing = [
        stage.splitlines()[0]
        for stage in npm_stages
        if not re.search(r"NPM_CONFIG_UPDATE_NOTIFIER=false", stage)
    ]
    assert missing == [], (
        f"{dockerfile}: npm stages without NPM_CONFIG_UPDATE_NOTIFIER=false: {missing}"
    )


# --- Real merge: what each deployment shape actually resolves to -----------------------

#: Profile-gated services (gpu-scale, gpu-split) are resolved too via ``--profile *``, except
#: for offline, whose overlay does not give the gpu-split services an image.
COMBOS: dict[str, tuple[str, ...]] = {
    "dev": ("docker-compose.yml", "docker-compose.override.yml"),
    "prod": ("docker-compose.yml", "docker-compose.prod.yml"),
    "prod+lite": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.lite.yml"),
    "prod+gpu-scale": (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.gpu-scale.yml",
    ),
    "prod+diar-native": (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.diar-native.yml",
    ),
    "offline": ("docker-compose.yml", "docker-compose.offline.yml"),
}


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not present")
@pytest.mark.parametrize("combo", sorted(COMBOS))
def test_resolved_config_has_telemetry_off(combo: str, tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in BACKEND_TELEMETRY_OFF}
    env.pop("MINIO_UPDATE", None)
    args = ["docker", "compose", "--env-file", os.devnull]
    for name in COMBOS[combo]:
        args += ["-f", name]
    if combo != "offline":
        args += ["--profile", "*"]
    args.append("config")
    result = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False, env=env
    )
    assert result.returncode == 0, result.stderr
    services = yaml.safe_load(result.stdout)["services"]
    checked = 0
    for name, service in services.items():
        resolved = service.get("environment") or {}
        if name == "minio":
            assert resolved.get("MINIO_UPDATE") == "off", f"{combo}:minio"
        if not _is_backend_image(service):
            continue
        checked += 1
        wrong = {
            k: resolved.get(k) for k, v in BACKEND_TELEMETRY_OFF.items() if resolved.get(k) != v
        }
        assert not wrong, f"{combo}:{name} resolves {wrong}"
    assert checked, f"{combo}: no backend-image service resolved"


# --- Code-level backstop: set before pyannote / huggingface_hub can be imported ---------

_ENTRY_POINTS = ("app/main.py", "app/core/celery.py")
_PRIVACY_MODULE = "app.core.privacy_env"


def _first_import(path: Path) -> str | None:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            return node.names[0].name
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            return f"{node.module}.{node.names[0].name}"
    return None


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
def test_entry_point_imports_privacy_env_first(entry_point: str) -> None:
    assert _first_import(BACKEND_DIR / entry_point) == _PRIVACY_MODULE, (
        f"{entry_point}: `from app.core import privacy_env` must be its first import so the telemetry "
        "opt-outs are in os.environ before anything can import pyannote/huggingface_hub"
    )


def _modules_importing(prefixes: tuple[str, ...]) -> list[Path]:
    hits = []
    for path in sorted((BACKEND_DIR / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == p or n.startswith(p + ".") for n in names for p in prefixes):
                hits.append(path)
                break
    return hits


def test_pyannote_import_sites_import_privacy_env() -> None:
    """Any module that imports pyannote.audio also pulls the backstop in at module level.

    Covers a process that reaches pyannote through a path other than the two entry points
    (a script, a REPL, a future entry point).
    """
    sites = _modules_importing(("pyannote.audio", "whisperx"))
    assert sites, "expected at least the diarizer to import pyannote.audio"
    missing = [
        str(p.relative_to(BACKEND_DIR))
        for p in sites
        if _PRIVACY_MODULE
        not in {
            f"{n.module}.{n.names[0].name}"
            for n in ast.parse(p.read_text()).body
            if isinstance(n, ast.ImportFrom)
        }
    ]
    assert not missing, f"must `from app.core import privacy_env` at module level: {missing}"


_ORDER_PROBE = textwrap.dedent(
    """
    import importlib.abc, os, sys
    WATCH = ("pyannote", "huggingface_hub", "transformers", "whisperx", "sentence_transformers")
    seen = []
    class Recorder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "app.core.privacy_env" or name.split(".")[0] in WATCH:
                seen.append((name, os.environ.get("PYANNOTE_METRICS_ENABLED"),
                             os.environ.get("HF_HUB_DISABLE_TELEMETRY")))
            return None
    sys.meta_path.insert(0, Recorder())
    import importlib
    importlib.import_module(sys.argv[1])
    print(repr(seen))
    print(repr({k: os.environ.get(k) for k in sys.argv[2:]}))
    """
)


@pytest.mark.parametrize("entry_module", ["app.core.celery", "app.main"])
def test_privacy_env_runs_before_any_watched_import(entry_module: str) -> None:
    env = {k: v for k, v in os.environ.items() if k not in BACKEND_TELEMETRY_OFF}
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env.setdefault("SKIP_CELERY", "true")
    result = subprocess.run(
        [sys.executable, "-c", _ORDER_PROBE, entry_module, *BACKEND_TELEMETRY_OFF],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        pytest.skip(f"{entry_module} not importable in this environment: {result.stderr[-400:]}")
    seen_line, env_line = result.stdout.strip().splitlines()[-2:]
    seen = ast.literal_eval(seen_line)
    assert seen and seen[0][0] == _PRIVACY_MODULE, (
        f"{entry_module}: first watched import was {seen[:1]}, not {_PRIVACY_MODULE}"
    )
    for name, pyannote_flag, hf_flag in seen[1:]:
        assert (pyannote_flag, hf_flag) == ("false", "1"), f"{name} imported with telemetry on"
    assert ast.literal_eval(env_line) == BACKEND_TELEMETRY_OFF


def test_privacy_env_respects_operator_choice() -> None:
    """setdefault, not overwrite: an operator who opts in keeps their choice."""
    env = {k: v for k, v in os.environ.items() if k not in BACKEND_TELEMETRY_OFF}
    env["PYANNOTE_METRICS_ENABLED"] = "true"
    env["PYTHONPATH"] = str(BACKEND_DIR)
    code = (
        "import os, app.core.privacy_env;"
        "print(os.environ['PYANNOTE_METRICS_ENABLED'], os.environ['HF_HUB_DISABLE_TELEMETRY'])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out == ["true", "1"]
