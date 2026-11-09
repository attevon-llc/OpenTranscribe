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


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or _env.get(name) or default


def _host_setting() -> str:
    """See ``cleanup-test-users.py``'s identical helper — same reasoning, duplicated
    rather than imported, so this script has no import-time dependency on that one
    (it invokes it as a subprocess instead, at the very end of the sweep)."""
    return os.environ.get('POSTGRES_HOST', 'localhost')


def _backend_url() -> str:
    return os.environ.get('E2E_BACKEND_URL', 'http://localhost:5174')


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
    db_port = _setting('POSTGRES_PORT', '5176')
    db_name = _setting('POSTGRES_DB', 'opentranscribe')
    return f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'


def _resolve_port(sweep: ModuleType, var: str) -> Any:
    """Env first, then ``.env``, then the stack default: the order ``_setting`` uses."""
    if os.environ.get(var):
        return sweep.ResolvedPort(var, int(os.environ[var]), 'env')
    if _env.get(var):
        return sweep.ResolvedPort(var, int(str(_env[var])), 'dotenv')
    return sweep.ResolvedPort(var, sweep.BASE_PORTS[var], 'default')


def _load_artifact_sweep() -> ModuleType:
    """``scripts/lib/test_artifact_sweep.py`` by path (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location('test_artifact_sweep', _ARTIFACT_SWEEP_MODULE)
    assert spec is not None and spec.loader is not None, f'cannot load {_ARTIFACT_SWEEP_MODULE}'
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module
