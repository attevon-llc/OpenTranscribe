#!/usr/bin/env python3
"""Licence gate: classify the licence of every Python and npm package the product ships.

The policy (ALLOW / CONCERN / BLOCKER) has ONE home, ``scripts/lib/license_policy.py``; this
script and ``scripts/lib/license_gate.py`` only read declared metadata, ask the policy, apply the
reasoned allowlist and write evidence. Other pipelines should import those two modules from the
pinned core rather than keep a copy.

Scope
    npm     production entries (no ``dev: true``) of ``frontend/package-lock.json``. The licence
            comes from the lockfile entry, else from the installed ``node_modules/<pkg>/package.json``
            next to it. ``--all-npm`` adds ``docs-site`` and the repo root (their lockfiles carry no
            licence for many entries and need ``npm ci`` first, so a missing one is UNKNOWN there).
    Python  the full resolved closure (transitives included) of the pip ``--report`` files written
            by ``scripts/check-requirements-resolve.py --report-dir DIR`` for the shipped
            requirements files (``requirements.txt``, ``requirements-nodeps.txt``,
            ``requirements-lite.txt``). Licence sources, in order: ``license_expression``,
            ``license``, then the ``License ::`` trove classifiers.
    NOT judged here: OS packages of the Debian/Alpine base images. The image SBOM at release covers
    them (the committed report lists 203 OS-package BLOCKERs that are base-image GPL packages,
    expected). Nor does it read licence texts, detect vendored code, or give legal advice.

Fail closed: UNKNOWN or missing licences, and BLOCKERs, fail unless
``scripts/licenses-allowlist.txt`` approves that exact ``ecosystem:name`` AND licence string with a
written reason (a relicense invalidates the approval). A stale allowlist line also fails.

Exit codes: 0 pass, 1 policy violation, 2 unusable input (missing/empty/unparseable file,
zero packages, malformed allowlist).

Usage::

    scripts/check-licenses.py --npm-only                       # offline, no pip: the hook path
    scripts/check-licenses.py --pip-report-dir out/head        # full gate
    scripts/check-licenses.py --verify-report-version          # committed report vs VERSION

Evidence (``license-gate.md``, ``.json``, ``.csv``) is written to ``--out-dir`` when scanning.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
import license_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = REPO_ROOT / 'scripts' / 'licenses-allowlist.txt'
REPORT_SUMMARY = REPO_ROOT / 'security-reports' / 'license-compliance' / 'SBOM-SUMMARY.md'
SHIPPED_REPORTS = ('requirements.txt', 'requirements-nodeps.txt', 'requirements-lite.txt')
FRONTEND_LOCK = 'frontend/package-lock.json'
EXTRA_LOCKS = ('docs-site/package-lock.json', 'package-lock.json')


def _area(lock: Path, root: Path) -> str:
    parent = lock.resolve().parent
    return 'root' if parent == root.resolve() else parent.name


def _collect(args: argparse.Namespace) -> tuple[list[gate.Package], list[str]]:
    root: Path = args.root
    packages: list[gate.Package] = []
    scanned: list[str] = []
    locks = list(args.lockfile) or [root / FRONTEND_LOCK]
    if args.all_npm:
        locks += [root / name for name in EXTRA_LOCKS]
    for lock in locks:
        packages += gate.load_npm_lock(lock, _area(lock, root))
    scanned.append('npm')
    if not args.npm_only:
        reports = list(args.pip_report)
        if args.pip_report_dir:
            reports += [args.pip_report_dir / f'{n}.json' for n in SHIPPED_REPORTS]
            if not (args.pip_report_dir / 'requirements.txt.json').is_file():
                raise gate.InputError(f'{args.pip_report_dir}: no requirements.txt.json')
            reports = [r for r in reports if r.is_file()]
        if not reports:
            raise gate.InputError(
                'the Python leg needs pip reports (--pip-report-dir or --pip-report); '
                'use --npm-only for the offline npm check'
            )
        seen: set[tuple[str, str, str]] = set()
        for report in reports:
            for pkg in gate.load_pip_report(report):
                ident = (pkg.name.lower(), pkg.version, pkg.license)
                if ident not in seen:
                    seen.add(ident)
                    packages.append(pkg)
        scanned.append('pip')
    return packages, scanned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument('--root', type=Path, default=REPO_ROOT, help='repository root')
    parser.add_argument('--npm-only', action='store_true', help='skip the Python leg (offline)')
    parser.add_argument('--all-npm', action='store_true', help='also docs-site and the repo root')
    parser.add_argument(
        '--lockfile',
        type=Path,
        action='append',
        default=[],
        help='npm lockfile (replaces the default)',
    )
    parser.add_argument(
        '--pip-report-dir',
        type=Path,
        help='directory of pip reports from check-requirements-resolve.py',
    )
    parser.add_argument(
        '--pip-report', type=Path, action='append', default=[], help='one pip --report JSON file'
    )
    parser.add_argument('--allowlist', type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument('--out-dir', type=Path, default=Path('license-evidence'))
    parser.add_argument(
        '--verify-report-version',
        action='store_true',
        help='check the committed report against VERSION',
    )
    parser.add_argument(
        '--report', type=Path, default=REPORT_SUMMARY, help='SBOM-SUMMARY.md to verify'
    )
    parser.add_argument(
        '--version-file', type=Path, default=None, help='VERSION file (default: <root>/VERSION)'
    )
    args = parser.parse_args(argv)
    scanning = (
        not args.verify_report_version
        or args.npm_only
        or bool(args.lockfile or args.pip_report or args.pip_report_dir or args.all_npm)
    )

    try:
        failures: list[str] = []
        if args.verify_report_version:
            version_file = args.version_file or args.root / 'VERSION'
            failures += gate.verify_report_version(args.report, version_file)
        if scanning:
            packages, scanned = _collect(args)
            allowlist = gate.load_allowlist(args.allowlist)
            result = gate.evaluate(packages, allowlist, scanned)
            failures += result.errors
            notes = [
                f'Ecosystems scanned: {", ".join(scanned)}.',
                'OS packages of the base images are not judged here (image SBOM at release).',
            ]
            args.out_dir.mkdir(parents=True, exist_ok=True)
            (args.out_dir / 'license-gate.md').write_text(
                gate.render_markdown(result, notes), encoding='utf-8'
            )
            (args.out_dir / 'license-gate.json').write_text(
                gate.render_json(
                    result, {'ecosystems_scanned': scanned, 'allowlist': str(args.allowlist.name)}
                ),
                encoding='utf-8',
            )
            (args.out_dir / 'license-gate.csv').write_text(
                gate.render_csv(result), encoding='utf-8'
            )
            counts = ', '.join(
                f'{cat}={sum(1 for r in result.rows if r.category == cat)}' for cat in gate.SEVERITY
            )
            sys.stderr.write(
                f'licence gate: {len(result.rows)} package(s) ({counts}); evidence in {args.out_dir}\n'
            )
    except gate.InputError as exc:
        sys.stderr.write(f'ERROR: {exc}\n')
        return 2

    if failures:
        sys.stderr.write('ERROR: licence gate failed.\n')
        for failure in failures:
            sys.stderr.write(f'  {failure}\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
