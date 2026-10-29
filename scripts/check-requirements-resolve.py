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
        # also resolves requirements.txt: it is the reference the drift check needs
    python3 scripts/check-requirements-resolve.py --report-dir out/head
        # keeps each file's raw pip report (<file>.json) for scripts/dependency-diff.py
    python3 scripts/check-requirements-resolve.py --backend-dir /path/to/other/checkout/backend
        # resolve another checkout's files (a worktree of the base ref), same pip, same flags

Run it with the interpreter that matches the images (3.13); a different minor can resolve
differently. Exit codes: 0 pass, 1 conflict or drift, 2 usage / pip too old.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The pip dry-run machinery lives in scripts/lib/requirements_resolve.py, shared with
# scripts/dependency-diff.py (scripts/ is not a package, so put its lib/ on the path).
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from requirements_resolve import BACKEND, STEPS, Outcome, base_version, resolve

#: Files whose resolved set must agree with requirements.txt wherever both resolve a package.
PARITY_FILES = ('requirements-lite.txt', 'requirements-ci.txt', 'requirements-dev.txt')

#: Packages allowed to differ from prod, each with the reason. A decision, not a backlog.
PARITY_EXEMPT: dict[str, str] = {
    'pyannote-audio': (
        'prod installs our GPU fork from a git SHA; the lite image deliberately installs '
        'the PyPI release for its ONNX export toolchain (see requirements-lite.txt)'
    ),
}


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


def select_steps(only: list[str] | None) -> list[tuple[str, tuple[str, ...]]]:
    """The steps to run for ``--only``; every step when it is not given.

    The drift check compares a variant against what ``requirements.txt`` resolves, so a
    selection that names a parity file pulls ``requirements.txt`` in as the reference.
    Without that, ``--only requirements-lite.txt`` resolved lite and then skipped the
    drift comparison without a word, which reads as "matches prod".
    """
    if not only:
        return list(STEPS)
    wanted = set(only)
    if wanted & set(PARITY_FILES):
        wanted.add(STEPS[0][0])
    return [step for step in STEPS if step[0] in wanted]


def main(argv: list[str] | None = None) -> int:
    """Resolve each requirements file, then check lite/CI/dev against prod."""
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--only', action='append', metavar='FILE', help='limit to these files')
    parser.add_argument(
        '--report-dir', type=Path, metavar='DIR', help="keep each file's pip report here"
    )
    parser.add_argument(
        '--backend-dir', type=Path, metavar='DIR', help='resolve the files under this backend/'
    )
    parser.add_argument(
        '--no-drift-check',
        action='store_true',
        help='only prove each file resolves; skip the lite/CI/dev comparison with requirements.txt',
    )
    args = parser.parse_args(argv)

    steps = select_steps(args.only)
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
    if args.report_dir:
        args.report_dir = args.report_dir.resolve()
        args.report_dir.mkdir(parents=True, exist_ok=True)
    backend = (args.backend_dir or BACKEND).resolve()
    with tempfile.TemporaryDirectory(prefix='ot-reqs-') as tmp:
        workdir = args.report_dir or Path(tmp)
        for name, extra in steps:
            print(f'==> resolving {name} {" ".join(extra)}'.rstrip(), flush=True)
            outcome = resolve(name, extra, workdir, backend)
            outcomes[name] = outcome
            if outcome.ok:
                print(f'    OK ({len(outcome.resolved)} packages)')
            else:
                print(f'    FAILED\n{outcome.detail}')
                failures.append(f'{name} does not resolve')

    reference = outcomes.get('requirements.txt')
    if reference and reference.ok and not args.no_drift_check:
        for name in PARITY_FILES:
            variant = outcomes.get(name)
            if not variant or not variant.ok:
                continue
            drift = find_drift(reference.resolved, variant.resolved)
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
