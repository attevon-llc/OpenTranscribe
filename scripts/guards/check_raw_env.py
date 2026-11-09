#!/usr/bin/env python3
"""Ratchet guard: raw process-environment reads in ``backend/app``.

Settings belong behind ``app/core/config.py``. Every other module that reaches into the process
environment directly (``os.getenv``, ``os.environ[...]``, ``os.environ.get``, ``os.environ.setdefault``,
``from os import environ``, ``import os as _os`` ...) is counted per file and recorded in
``scripts/guards/baselines/raw_env.txt``. The baseline can only shrink.

Counting is AST based, so mentions inside comments, docstrings and string literals are ignored,
and function bodies are walked like any other code. Each reference to an ``os`` environment
attribute counts once, so ``os.environ.get("X")`` is one site, not two. A file that does not
parse fails the run (exit 2) rather than being skipped.

Reference count when the baseline was written: 253 sites in 86 files, made of ``os.getenv`` 139,
``os.environ.get`` 93, ``os.environ[...]`` 10, ``os.environ.setdefault`` 4, bare ``os.environ``
in a conditional expression 4 and bare ``os.environ`` passed as an argument 3.

Usage::

    check_raw_env.py [--root DIR] [--baseline FILE]
    check_raw_env.py --tighten
    check_raw_env.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline as bl

_HERE = Path(__file__).resolve().parent
_APP = Path('backend') / 'app'
_DEFAULT_BASELINE = _HERE / 'baselines' / 'raw_env.txt'

# Repo-relative paths allowed to read the environment directly, each with the reason.
EXEMPT: dict[str, str] = {
    'backend/app/core/config.py': 'the sanctioned settings module; the one place env is read',
    # Settings fields moved verbatim out of config.py (v0.7.0 L07); `Settings` inherits them, so
    # these classes are the declaration half of the same sanctioned settings module.
    'backend/app/core/settings_identity.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/settings_providers.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/settings_search.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/settings_security.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/settings_storage.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/env.py': 'the sanctioned reader for tuning knobs (D4 policy); every other module calls it',
}

# Classified exemptions. A reason starts with its class:
#   bootstrap-before-settings  runs before ``Settings`` can be built (Celery/worker boot, migrations,
#                              the env-name compat readers settings itself calls), so it cannot go
#                              through settings or ``app.core.env`` without a cycle or a late read.
#   process-env-handoff        builds or mutates the environment handed to a child process or to a
#                              third-party library (HF_TOKEN, CUDA_VISIBLE_DEVICES, OMP threads);
#                              the process environment IS the interface, not a setting.
#   cli-script                 operator/benchmark entry points under app/scripts, not served code.
_BOOTSTRAP = 'bootstrap-before-settings'
_HANDOFF = 'process-env-handoff'
_CLI = 'cli-script'
EXEMPT.update(
    {
        'backend/app/core/celery.py': f'{_BOOTSTRAP}: broker TLS and pool mode are read while the Celery app is built',
        'backend/app/core/celery_worker_lifecycle.py': f'{_HANDOFF}: pins OMP/torch thread env at worker boot before torch loads',
        'backend/app/core/privacy_env.py': f'{_BOOTSTRAP}: setdefault of telemetry opt-outs before any import can read them',
        'backend/app/core/legacy_auth_env.py': f'{_BOOTSTRAP}: the OIDC env-name compat reader that Settings itself calls',
        'backend/app/core/worker_metrics.py': f'{_BOOTSTRAP}: prometheus multiprocess dir must exist before prometheus_client imports',
        'backend/app/core/worker_shutdown.py': f'{_BOOTSTRAP}: module-level budget read at import by the worker signal handler',
        'backend/app/db/migrations.py': f'{_BOOTSTRAP}: the startup DB wait runs before the app config is usable',
        'backend/app/services/backup_service.py': f'{_HANDOFF}: copies the environment into the pg_dump/gpg subprocess',
        'backend/app/services/local_models/supervisor.py': f'{_HANDOFF}: copies the environment into the model-server subprocess',
        'backend/app/services/local_models/runner.py': f'{_HANDOFF}: HF_TOKEN read in the child process that downloads models',
        'backend/app/services/huggingface_token.py': f'{_HANDOFF}: publishes the admin token as HF_TOKEN for huggingface_hub',
        'backend/app/services/asr/google_provider.py': f'{_HANDOFF}: GOOGLE_APPLICATION_CREDENTIALS is the google-cloud client interface',
        'backend/app/services/chat/reranker_threads.py': f'{_HANDOFF}: respects an operator OMP/torch thread pin before torch sizes its pool',
        'backend/app/transcription/native_provision.py': f'{_HANDOFF}: environment passed to the diar-native provisioning subprocess',
        'backend/app/transcription/engine/engine.py': f'{_HANDOFF}: extends PYTORCH_CUDA_ALLOC_CONF before torch initialises CUDA',
        'backend/app/utils/nltk_offline.py': f'{_HANDOFF}: NLTK_OFFLINE/NLTK_DATA are the nltk library interface',
        'backend/app/scripts/benchmark_reranker_threads.py': f'{_CLI}: benchmark entry point',
        'backend/app/scripts/build_password_blocklist.py': f'{_CLI}: operator build script',
        'backend/app/scripts/corpus_injection/adapters/miracl.py': f'{_CLI}: corpus tooling',
        'backend/app/scripts/corpus_injection/env.py': f'{_CLI}: corpus tooling .env loader',
        'backend/app/scripts/gpu_stage_vram_probe.py': f'{_CLI}: GPU probe sets its own environment',
        'backend/app/scripts/probe_torch_thread_scope.py': f'{_CLI}: diagnostic probe',
    }
)

# Attributes of the ``os`` module that read or mutate the process environment.
_ENV_NAMES = frozenset({'getenv', 'getenvb', 'environ', 'environb', 'putenv', 'unsetenv'})


def count_sites(tree: ast.AST) -> int:
    """Number of raw environment references in ``tree``, following ``os`` and member aliases."""
    os_names: set[str] = set()
    env_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'os':
                    os_names.add(alias.asname or 'os')
                elif alias.name.startswith('os.') and alias.asname is None:
                    os_names.add('os')
        elif isinstance(node, ast.ImportFrom) and node.module == 'os' and node.level == 0:
            for alias in node.names:
                if alias.name in _ENV_NAMES:
                    env_names.add(alias.asname or alias.name)
    total = 0
    for node in ast.walk(tree):
        is_os_attr = (
            isinstance(node, ast.Attribute)
            and node.attr in _ENV_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in os_names
        )
        is_member_alias = isinstance(node, ast.Name) and node.id in env_names
        if is_os_attr or is_member_alias:
            total += 1
    return total


def scan(root: Path) -> dict[str, int]:
    """Map repo-relative path -> site count for every non-exempt file with at least one site."""
    app = root / _APP
    if not app.is_dir():
        msg = f'{app} is not a directory; pass --root explicitly'
        raise bl.ScanError(msg)
    findings: dict[str, int] = {}
    for path in sorted(app.rglob('*.py')):
        rel = path.relative_to(root).as_posix()
        if rel in EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            msg = f'cannot parse {rel}: {exc}'
            raise bl.ScanError(msg) from exc
        count = count_sites(tree)
        if count:
            findings[rel] = count
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument(
        '--root', type=Path, default=None, help='repository root (default: found from this file)'
    )
    parser.add_argument('--baseline', type=Path, default=None, help='baseline file')
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        root = args.root or bl.find_root(_HERE, ('backend/app',))
        baseline_path = args.baseline or (root / 'scripts' / 'guards' / 'baselines' / 'raw_env.txt')
        findings = scan(root)
        if args.write_baseline:
            written: int = bl.run_write_baseline(args, baseline_path, findings)
            return written
        baseline = bl.load(baseline_path)
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    return bl.finish_ratchet(
        args,
        baseline_path,
        findings,
        baseline,
        unit='raw env reads',
        heading='raw environment reads outside app/core/config.py:',
        ok_message=f'raw env: {sum(findings.values())} sites in {len(findings)} files, within the baseline',
    )


if __name__ == '__main__':
    sys.exit(main())
