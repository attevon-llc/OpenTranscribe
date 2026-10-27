#!/usr/bin/env python3
"""Prove every backend requirements file RESOLVES, and that the variants match prod.

Why this exists: ``backend/requirements.txt`` once pinned ``typer==0.27.3`` while its own
``huggingface-hub==1.17.0`` declares ``typer<0.26``. Nothing in CI ever resolved the file
(the CI job installs ``requirements-ci.txt`` and security-scan runs a non-blocking
``pip-audit``), so a file that could not be installed stayed green until an image build.

What it does, with ``pip install --dry-run`` (resolves and fetches metadata, installs
nothing, so it needs no venv of its own and no GPU):

1. ``requirements.txt``                      must resolve (the prod / dev-venv step 1)
2. ``requirements-nodeps.txt --no-deps``     must be installable (the second step; its
   packages are ``--no-deps`` on purpose, see that file, so this proves the pins exist
   and nothing more - it cannot see whisperx's own metadata caps, by design)
3. ``requirements-dev.txt``                  must resolve (includes requirements.txt)
4. ``requirements-lite.txt``                 must resolve (CPU image)
5. ``requirements-ci.txt``                   must resolve (GitHub Actions)

Then it compares what lite and CI would install against what prod would install and fails
on any package resolved at a DIFFERENT version (a ``+cpu``/``+cu128`` local tag is not a
different version). That is the check that catches "CI tests starlette 1.10.0 while the
image ships 1.9.0".

Usage::

    python3 scripts/check-requirements-resolve.py            # all files
    python3 scripts/check-requirements-resolve.py --only requirements-lite.txt

Run it with the interpreter that matches the images (3.13); a different minor can resolve
differently. Exit codes: 0 pass, 1 conflict or drift, 2 usage / pip too old.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / 'backend'

#: ``(file, extra pip args)`` in install order. ``requirements.txt`` must stay first: the
#: drift check uses it as the reference.
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('requirements.txt', ()),
    ('requirements-nodeps.txt', ('--no-deps',)),
    ('requirements-dev.txt', ()),
    ('requirements-lite.txt', ()),
    ('requirements-ci.txt', ()),
)

#: Files whose resolved set must agree with requirements.txt wherever both resolve a package.
PARITY_FILES = ('requirements-lite.txt', 'requirements-ci.txt', 'requirements-dev.txt')

#: Packages allowed to differ from prod, each with the reason. A decision, not a backlog.
PARITY_EXEMPT: dict[str, str] = {
    'pyannote-audio': (
        'prod installs our GPU fork from a git SHA; the lite image deliberately installs '
        'the PyPI release for its ONNX export toolchain (see requirements-lite.txt)'
    ),
}


@dataclass(frozen=True)
class Outcome:
    """Result of resolving one requirements file."""

    name: str
    ok: bool
    resolved: dict[str, str]
    detail: str


def normalise(name: str) -> str:
    """PEP 503 name normalisation (``pyannote.audio`` -> ``pyannote-audio``)."""
    return re.sub(r'[-_.]+', '-', name).lower()


def resolved_versions(report: dict[str, object]) -> dict[str, str]:
    """``normalised name -> version`` for every package a pip ``--report`` would install."""
    install = report.get('install')
    if not isinstance(install, list):
        raise ValueError("pip report has no 'install' list")
    found: dict[str, str] = {}
    for item in install:
        meta = item['metadata']
        found[normalise(str(meta['name']))] = str(meta['version'])
    return found


def base_version(version: str) -> str:
    """Drop a PEP 440 local tag: ``2.11.0+cpu`` and ``2.11.0`` are the same upstream release."""
    return version.split('+', 1)[0]


def find_drift(
    reference: dict[str, str],
    variant: dict[str, str],
    exempt: dict[str, str] = PARITY_EXEMPT,
) -> list[str]:
    """Describe every package ``variant`` resolves at a different version from ``reference``."""
    drift = []
    for name in sorted(variant):
        if name in exempt or name not in reference:
            continue
        if base_version(variant[name]) != base_version(reference[name]):
            drift.append(f'{name}: prod={reference[name]} variant={variant[name]}')
    return drift


def _conflict_excerpt(output: str) -> str:
    """The part of pip's output a human needs: the conflict block, else the last lines."""
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if 'conflict is caused by' in line or line.startswith('ERROR:'):
            return '\n'.join(lines[index:][:25])
    return '\n'.join(lines[-25:])


def resolve(name: str, extra: tuple[str, ...], workdir: Path) -> Outcome:
    """Run ``pip install --dry-run`` for one requirements file."""
    report = workdir / f'{name}.json'
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
        str(BACKEND / name),
    ]
    # cwd is the backend dir so relative `-r` includes inside a file resolve.
    proc = subprocess.run(command, cwd=BACKEND, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return Outcome(name, False, {}, _conflict_excerpt(proc.stdout + proc.stderr))
    return Outcome(name, True, resolved_versions(json.loads(report.read_text())), '')


def main(argv: list[str] | None = None) -> int:
    """Resolve each requirements file, then check lite/CI/dev against prod."""
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--only', action='append', metavar='FILE', help='limit to these files')
    args = parser.parse_args(argv)

    steps = [s for s in STEPS if not args.only or s[0] in args.only]
    unknown = set(args.only or ()) - {s[0] for s in STEPS}
    if unknown:
        print(f'unknown requirements file(s): {sorted(unknown)}', file=sys.stderr)
        return 2
    version = subprocess.run(
        [sys.executable, '-m', 'pip', '--version'], capture_output=True, text=True, check=False
    ).stdout
    match = re.search(r'pip (\d+)\.(\d+)', version)
    if not match or (int(match[1]), int(match[2])) < (22, 2):
        print(f'pip >= 22.2 is required for --dry-run --report (found: {version.strip()!r})')
        return 2

    failures: list[str] = []
    outcomes: dict[str, Outcome] = {}
    with tempfile.TemporaryDirectory(prefix='ot-reqs-') as tmp:
        for name, extra in steps:
            print(f'==> resolving {name} {" ".join(extra)}'.rstrip(), flush=True)
            outcome = resolve(name, extra, Path(tmp))
            outcomes[name] = outcome
            if outcome.ok:
                print(f'    OK ({len(outcome.resolved)} packages)')
            else:
                print(f'    FAILED\n{outcome.detail}')
                failures.append(f'{name} does not resolve')

    reference = outcomes.get('requirements.txt')
    if reference and reference.ok:
        for name in PARITY_FILES:
            outcome = outcomes.get(name)
            if not outcome or not outcome.ok:
                continue
            drift = find_drift(reference.resolved, outcome.resolved)
            if drift:
                print(f'==> {name} resolves different versions from requirements.txt:')
                print('    ' + '\n    '.join(drift))
                failures.append(f'{name} drifts from requirements.txt ({len(drift)} packages)')
            else:
                print(f'==> {name} matches requirements.txt on every shared package')

    if failures:
        print('\nFAIL:\n  - ' + '\n  - '.join(failures))
        return 1
    print('\nAll requirements files resolve and agree with requirements.txt.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
