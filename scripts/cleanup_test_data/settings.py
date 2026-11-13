"""Environment settings, repo paths and the sibling-script loaders."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[2]


_CLEANUP_USERS_SCRIPT = _REPO_ROOT / 'scripts' / 'cleanup-test-users.py'


_ARTIFACT_SWEEP_MODULE = _REPO_ROOT / 'scripts' / 'lib' / 'test_artifact_sweep.py'


_env = dotenv_values(_REPO_ROOT / '.env')


def _stack_target() -> ModuleType:
    sys.path.insert(0, str(_REPO_ROOT / 'scripts' / 'lib'))
    import stack_target

    return stack_target


def _setting(name: str, default: str) -> str:
    return str(_stack_target().env_setting(name, default, _env))


def _host_setting() -> str:
    """See ``cleanup-test-users.py``'s identical helper — same reasoning, duplicated
    rather than imported, so this script has no import-time dependency on that one
    (it invokes it as a subprocess instead, at the very end of the sweep)."""
    return os.environ.get('POSTGRES_HOST', 'localhost')


def _backend_url() -> str:
    explicit = os.environ.get('E2E_BACKEND_URL')
    if explicit:
        return explicit
    port = _stack_target().observed_port('BACKEND_PORT')
    if not port:
        raise SystemExit(
            'stack-target: no backend port resolved (export E2E_BACKEND_URL or start a stack)'
        )
    return f'http://localhost:{port}'


def _admin_email() -> str:
    return os.environ.get('E2E_ADMIN_EMAIL', 'admin@example.com')


def _admin_password() -> str:
    return os.environ.get('E2E_ADMIN_PASSWORD', 'password')  # documented dev default


def _load_cleanup_users_module() -> ModuleType:
    """Load ``cleanup-test-users.py`` by path (hyphenated filename blocks a normal
    import) — same pattern as its own unit test's ``_load_script``."""
    spec = importlib.util.spec_from_file_location('cleanup_test_users', _CLEANUP_USERS_SCRIPT)
    assert spec is not None and spec.loader is not None, f'cannot load {_CLEANUP_USERS_SCRIPT}'
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _engine_url() -> str:
    db_user = _setting('POSTGRES_USER', 'postgres')
    db_password = _setting('POSTGRES_PASSWORD', 'postgres')
    db_host = _host_setting()
    db_port = _setting('POSTGRES_PORT', '')
    db_name = _setting('POSTGRES_DB', 'opentranscribe')
    return f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'


def _resolve_port(sweep: ModuleType, var: str) -> Any:
    """Env first, then the resolved stack's published port, then ``.env``."""
    if os.environ.get(var):
        return sweep.ResolvedPort(var, int(os.environ[var]), 'env')
    observed = _stack_target().observed_port(var)
    if observed:
        return sweep.ResolvedPort(var, int(observed), 'stack')
    if _env.get(var):
        return sweep.ResolvedPort(var, int(str(_env[var])), 'dotenv')
    raise SystemExit(f'stack-target: no {var} resolved (nothing exported, no stack, no .env)')


def _load_artifact_sweep() -> ModuleType:
    """``scripts/lib/test_artifact_sweep.py`` by path (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location('test_artifact_sweep', _ARTIFACT_SWEEP_MODULE)
    assert spec is not None and spec.loader is not None, f'cannot load {_ARTIFACT_SWEEP_MODULE}'
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module
