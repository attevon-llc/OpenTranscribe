#!/usr/bin/env python3
"""Ratchet for frontend Stylelint findings (global CSS and Svelte ``<style>`` blocks).

Runs ``npx stylelint`` in ``frontend/`` (config ``frontend/stylelint.config.mjs``) and counts
findings per ``path::rule`` (path relative to the repo root). The counts are compared with
``baselines/stylelint.txt``: they may only shrink. Never baselined, always a failure: a CSS
syntax error, a parse error, an invalid rule option (it means a rule is silently not running)
and the disable-comment reports (needless, invalid-scope or description-less disables).
Output that is not the expected JSON fails closed (exit 2).

With FILES, plain ``.css`` files are first run through ``stylelint --fix`` (never ``.svelte``);
if that rewrote any, the guard exits 1 so the hook says "re-stage" (prettier's contract).
With FILES (the pre-commit commit stage passes the staged paths) only those files are linted
and only their baseline entries are compared. Without FILES the whole ``src/`` tree is.

Usage:
    check_stylelint_ratchet.py [FILES ...] [--root DIR] [--baseline FILE]
                               [--stylelint-json PATH] [--timeout SEC] [--tighten]
    check_stylelint_ratchet.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline as bl

_HERE = Path(__file__).resolve().parent
_DEFAULT_BASELINE = _HERE / 'baselines' / 'stylelint.txt'
_WHOLE_TREE = 'src/**/*.{css,svelte}'
_SUFFIXES = ('.css', '.svelte')
DEFAULT_TIMEOUT = 600
#: Findings a baseline may never hold. The ``--report-*`` ids are what Stylelint reports for the
#: config's reportNeedlessDisables / reportInvalidScopeDisables / reportDescriptionlessDisables.
NEVER_BASELINED = frozenset(
    {
        'CssSyntaxError',
        '--report-needless-disables',
        '--report-invalid-scope-disables',
        '--report-descriptionless-disables',
    }
)


def _run_stylelint(frontend: Path, targets: list[str], timeout: int) -> str:
    """Run stylelint over ``targets`` and return its JSON report. Exit 0 and 2 are results."""
    if not frontend.is_dir():
        raise bl.ScanError(f'{frontend} is not a directory')
    with tempfile.TemporaryDirectory(prefix='ot-stylelint-') as tmp:
        report = Path(tmp) / 'report.json'
        argv = ['npx', '--no-install', 'stylelint', *targets, '-f', 'json', '-o', str(report)]
        try:
            proc = subprocess.run(
                argv, cwd=frontend, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise bl.ScanError(f'stylelint did not run: {exc}') from exc
        if proc.returncode not in (0, 2) or not report.is_file():
            detail = (proc.stderr or proc.stdout).strip()[:500]
            raise bl.ScanError(f'stylelint exited {proc.returncode}: {detail}')
        return report.read_text(encoding='utf-8')


def autofix_css(frontend: Path, targets: list[str], timeout: int) -> list[str]:
    """Run ``stylelint --fix`` over the plain ``.css`` among ``targets``; return those it changed.

    Plain CSS only: ``--fix`` on ``.svelte`` goes through postcss-html and is not trusted to
    round-trip markup. Returns ``frontend``-relative paths whose bytes differ afterwards.
    """
    css = [t for t in targets if t.endswith('.css')]
    if not css:
        return []
    before = {t: (frontend / t).read_bytes() for t in css}
    argv = ['npx', '--no-install', 'stylelint', '--fix', *css]
    try:
        # exit 2 = unfixable findings remain; the ratchet below reports those
        subprocess.run(
            argv, cwd=frontend, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise bl.ScanError(f'stylelint --fix did not run: {exc}') from exc
    return [t for t in css if (frontend / t).read_bytes() != before[t]]


def _rel(source: str, root: Path) -> str:
    path = Path(source)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def parse(raw: str, root: Path) -> tuple[dict[str, int], list[str]]:
    """Return ``(finding counts by path::rule, hard error lines)`` from stylelint JSON output."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise bl.ScanError(f'stylelint output is not JSON: {exc}') from exc
    if not isinstance(data, list):
        raise bl.ScanError('stylelint JSON output is not a list of file results')
    counts: Counter[str] = Counter()
    errors: list[str] = []
    for result in data:
        if not isinstance(result, dict) or not isinstance(result.get('warnings'), list):
            raise bl.ScanError('stylelint JSON entry lacks warnings')
        rel = _rel(str(result.get('source', '<input>')), root)
        for opt in result.get('invalidOptionWarnings') or []:
            errors.append(f'{rel}: invalid stylelint option: {opt.get("text", opt)}')
        for perr in result.get('parseErrors') or []:
            errors.append(f'{rel}:{perr.get("line")}: parse error: {perr.get("text", perr)}')
        for msg in result['warnings']:
            rule = msg.get('rule')
            if not isinstance(rule, str):
                raise bl.ScanError(f'stylelint finding without a rule in {rel}: {msg!r}')
            if rule in NEVER_BASELINED:
                errors.append(f'{rel}:{msg.get("line")}: {rule}: {msg.get("text")}')
            else:
                counts[f'{rel}::{rule}'] += 1
    return dict(counts), errors


def _targets(files: list[str], root: Path) -> list[str]:
    """Frontend-relative paths of the CSS/Svelte files under ``frontend/src`` among ``files``."""
    frontend = (root / 'frontend').resolve()
    picked: list[str] = []
    for name in files:
        path = (root / name).resolve()
        try:
            rel = path.relative_to(frontend).as_posix()
        except ValueError:
            continue
        if rel.startswith('src/') and rel.endswith(_SUFFIXES) and path.is_file():
            picked.append(rel)
    return picked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('files', nargs='*', help='lint only these repo-relative paths')
    parser.add_argument('--root', type=Path, default=None, help='repo root (default: auto-detect)')
    parser.add_argument('--baseline', type=Path, default=_DEFAULT_BASELINE)
    parser.add_argument('--stylelint-json', type=Path, default=None, help='read a prior report')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    if args.files and (args.tighten or args.write_baseline):
        parser.error('--tighten and --write-baseline work on the whole tree; drop the FILES')

    try:
        root = args.root or bl.find_root(_HERE, ('frontend/stylelint.config.mjs',))
        scope: set[str] | None = None
        if args.files:
            targets = _targets(args.files, root)
            if not targets:
                print('stylelint ratchet: no frontend/src CSS or Svelte files given')
                return 0
            scope = {f'frontend/{t}' for t in targets}
        else:
            targets = [_WHOLE_TREE]
        if scope is not None and not args.stylelint_json:
            fixed = autofix_css(root / 'frontend', targets, args.timeout)
            if fixed:
                print(
                    'stylelint --fix rewrote these CSS files; re-stage them and commit again:',
                    file=sys.stderr,
                )
                print('\n'.join(f'  frontend/{t}' for t in fixed), file=sys.stderr)
                return 1
        if args.stylelint_json:
            raw = args.stylelint_json.read_text(encoding='utf-8')
        else:
            raw = _run_stylelint(root / 'frontend', targets, args.timeout)
        current, errors = parse(raw, root)
        if args.write_baseline:
            if errors:
                print('\n'.join(errors), file=sys.stderr)
                print(
                    'error: syntax errors and disable reports are never baselined', file=sys.stderr
                )
                return 1
            written: int = bl.run_write_baseline(args, args.baseline, current)
            return written
        baseline = bl.load(args.baseline)
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    if scope is not None:
        baseline = {k: v for k, v in baseline.items() if k.split('::', 1)[0] in scope}
    if args.tighten and not errors:
        problems = bl.compare(current, baseline, unit='findings')
        if any(k not in baseline or current[k] > baseline[k].value for k in current):
            print('\n'.join(problems), file=sys.stderr)
            return 1
        print(f'tightened {bl.tighten(args.baseline, current, baseline)} baseline entries')
        return 0
    problems = errors + bl.compare(current, baseline, unit='findings')
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        print(f'{len(problems)} stylelint ratchet problem(s)', file=sys.stderr)
        return 1
    print(f'stylelint ratchet ok: {sum(current.values())} findings in {len(current)} buckets')
    return 0


if __name__ == '__main__':
    sys.exit(main())
