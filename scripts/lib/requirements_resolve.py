"""Resolve a backend requirements file with ``pip install --dry-run --report``, stdlib only.

Shared by ``scripts/check-requirements-resolve.py`` (does every file resolve, do the variants
agree with prod) and ``scripts/dependency-diff.py`` (what is the transitive closure of a file at
two refs). One implementation of "run pip and read its report", so the two cannot disagree about
what a file resolves to.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / 'backend'

#: ``(file, extra pip args)`` in install order. ``requirements.txt`` must stay first: the
#: drift check uses it as the reference. The extra args are also the file's ROLE:
#: ``requirements-nodeps.txt`` is installed ``--no-deps``, so its pins are its whole closure.
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('requirements.txt', ()),
    ('requirements-nodeps.txt', ('--no-deps',)),
    ('requirements-dev.txt', ()),
    ('requirements-lite.txt', ()),
    ('requirements-ci.txt', ()),
)


@dataclass(frozen=True)
class Outcome:
    """Result of resolving one requirements file."""

    name: str
    ok: bool
    resolved: dict[str, str]
    detail: str


@dataclass(frozen=True)
class ResolvedPackage:
    """One package a pip ``--report`` would install."""

    version: str
    license: str | None


def normalise(name: str) -> str:
    """PEP 503 name normalisation (``pyannote.audio`` -> ``pyannote-audio``)."""
    return re.sub(r'[-_.]+', '-', name).lower()


def _install_items(report: dict[str, object]) -> list[dict[str, dict[str, object]]]:
    install = report.get('install')
    if not isinstance(install, list):
        raise ValueError("pip report has no 'install' list")
    return install


def resolved_versions(report: dict[str, object]) -> dict[str, str]:
    """``normalised name -> version`` for every package a pip ``--report`` would install."""
    found: dict[str, str] = {}
    for item in _install_items(report):
        meta = item['metadata']
        found[normalise(str(meta['name']))] = str(meta['version'])
    return found


def report_license(meta: dict[str, object]) -> str | None:
    """The licence string of one report ``metadata`` object, or ``None`` when it states none.

    Precedence: ``license_expression`` (PEP 639), then ``license``, then the ``License ::``
    trove classifiers joined with ``OR``. A free-text ``license`` that is a whole licence body
    rather than an identifier is passed through as-is: classifying it is the policy's job.
    """
    for field in ('license_expression', 'license'):
        value = meta.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    classifiers = meta.get('classifier')
    if isinstance(classifiers, list):
        names = sorted(
            {
                c.split('::')[-1].strip()
                for c in classifiers
                if isinstance(c, str) and c.startswith('License ::') and c.count('::') >= 2
            }
        )
        if names:
            return ' OR '.join(names)
    return None


def resolved_packages(report: dict[str, object]) -> dict[str, ResolvedPackage]:
    """``normalised name -> (version, licence)`` for every package a pip report would install."""
    found: dict[str, ResolvedPackage] = {}
    for item in _install_items(report):
        meta = item['metadata']
        found[normalise(str(meta['name']))] = ResolvedPackage(
            str(meta['version']), report_license(meta)
        )
    return found


def base_version(version: str) -> str:
    """Drop a PEP 440 local tag: ``2.11.0+cpu`` and ``2.11.0`` are the same upstream release."""
    return version.split('+', 1)[0]


def _conflict_excerpt(output: str) -> str:
    """The part of pip's output a human needs: the conflict block, else the last lines."""
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if 'conflict is caused by' in line or line.startswith('ERROR:'):
            return '\n'.join(lines[index:][:25])
    return '\n'.join(lines[-25:])


def resolve(name: str, extra: tuple[str, ...], workdir: Path, backend: Path = BACKEND) -> Outcome:
    """Run ``pip install --dry-run`` for one requirements file under ``backend``.

    The raw pip report is left at ``workdir/<name>.json``. ``backend`` defaults to this
    checkout's ``backend/``; a worktree of another ref passes its own.
    """
    # pip runs with cwd=backend, so a relative path would be re-based onto it.
    backend = backend.resolve()
    report = (workdir / f'{name}.json').resolve()
    command = [
        sys.executable,
        '-m',
        'pip',
        'install',
        '--dry-run',
        '--ignore-installed',
        '--disable-pip-version-check',
        '--quiet',
        '--report',
        str(report),
        *extra,
        '-r',
        str(backend / name),
    ]
    # cwd is the backend dir so relative `-r` includes inside a file resolve.
    proc = subprocess.run(command, cwd=backend, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return Outcome(name, False, {}, _conflict_excerpt(proc.stdout + proc.stderr))
    return Outcome(name, True, resolved_versions(json.loads(report.read_text())), '')
