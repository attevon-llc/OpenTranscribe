"""Logic of the dependency licence gate (``scripts/check-licenses.py``), stdlib only.

The policy itself (which licence is ALLOW / CONCERN / BLOCKER) lives in ``license_policy.py`` and
nowhere else. This module only turns declared package metadata into a licence string, parses SPDX
style expressions, asks the policy about each *leaf* licence, applies the reasoned allowlist and
renders the evidence. Nothing here knows a licence name's verdict.

Fail closed: a missing, empty or unrecognised licence is ``unknown`` and fails the gate unless the
allowlist carries it with a written reason.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import importlib.util
import io
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from license_policy import BLOCKER_SUBSTRINGS, CATEGORY_LABEL, classify_license_string

#: Least to most severe. ``unknown`` ranks above ``proprietary`` so it can never hide behind it.
SEVERITY = ('allow', 'concern', 'proprietary', 'unknown', 'blocker')
FAILING = ('blocker', 'unknown')

#: Free text longer than this is a licence body, not an identifier.
_MAX_IDENTIFIER = 120


class InputError(Exception):
    """The gate cannot run on what it was given (exit 2), as opposed to a policy violation."""


def _rank(category: str) -> int:
    return SEVERITY.index(category)


# --- expression classification -------------------------------------------------------------

#: Names that trove classifiers and free-text ``License`` fields use, spelled as the SPDX
#: identifier the policy knows. Spelling only: the verdict still comes from the policy.
ALIASES: dict[str, str] = {
    'mit license': 'MIT',
    'the mit license': 'MIT',
    'mit/x11 license': 'MIT',
    'bsd license': 'BSD',
    'bsd': 'BSD',
    'new bsd license': 'BSD-3-Clause',
    'bsd 3-clause license': 'BSD-3-Clause',
    '3-clause bsd license': 'BSD-3-Clause',
    'bsd 3-clause "new" or "revised" license': 'BSD-3-Clause',
    'simplified bsd license': 'BSD-2-Clause',
    'bsd 2-clause license': 'BSD-2-Clause',
    'apache software license': 'Apache-2.0',
    'apache license 2.0': 'Apache-2.0',
    'apache license, version 2.0': 'Apache-2.0',
    'apache 2.0 license': 'Apache-2.0',
    'apache-2': 'Apache-2.0',
    'isc license (iscl)': 'ISC',
    'isc license': 'ISC',
    'python software foundation license': 'PSF-2.0',
    'the unlicense (unlicense)': 'Unlicense',
    'zlib/libpng license': 'Zlib',
    'cc0 1.0 universal (cc0 1.0) public domain dedication': 'CC0-1.0',
    'mozilla public license 2.0 (mpl 2.0)': 'MPL-2.0',
    'mozilla public license 1.1 (mpl 1.1)': 'MPL-1.1',
    'gnu lesser general public license v2 (lgplv2)': 'LGPL-2.0-only',
    'gnu lesser general public license v2 or later (lgplv2+)': 'LGPL-2.0-or-later',
    'gnu lesser general public license v3 (lgplv3)': 'LGPL-3.0-only',
    'gnu lesser general public license v3 or later (lgplv3+)': 'LGPL-3.0-or-later',
    'gnu library or lesser general public license (lgpl)': 'LGPL-2.1-or-later',
    'gnu general public license (gpl)': 'GPL-2.0-or-later',
    'gnu general public license v2 (gplv2)': 'GPL-2.0-only',
    'gnu general public license v2 or later (gplv2+)': 'GPL-2.0-or-later',
    'gnu general public license v3 (gplv3)': 'GPL-3.0-only',
    'gnu general public license v3 or later (gplv3+)': 'GPL-3.0-or-later',
    'gnu affero general public license v3': 'AGPL-3.0-only',
    'gnu affero general public license v3 or later (agplv3+)': 'AGPL-3.0-or-later',
}

#: Values packaging tools write when they know nothing: not a licence statement.
PLACEHOLDERS = frozenset({'unknown', 'noassertion', 'none', 'n/a', 'other'})

_TOKEN = re.compile(r'\(|\)|[^\s()]+')
_OR_LATER = re.compile(r'\bor[ -]later\b', re.IGNORECASE)


def _worst(results: Iterable[tuple[str, str]]) -> tuple[str, str]:
    return max(results, key=lambda r: _rank(r[0]))


def _best(results: Iterable[tuple[str, str]]) -> tuple[str, str]:
    return min(results, key=lambda r: _rank(r[0]))


class _Parser:
    """Recursive descent over ``expr := term (OR term)*`` with ``AND`` binding tighter than ``OR``.

    A leaf is a run of non-keyword words; an ``WITH <exception>`` rider after it is dropped
    (the raw expression was already checked for the restricted riders, see :func:`classify`).
    """

    def __init__(self, text: str) -> None:
        self.tokens = _TOKEN.findall(text)
        self.pos = 0

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _peek_word(self) -> str | None:
        tok = self._peek()
        return tok.upper() if tok is not None else None

    def parse(self) -> tuple[str, str]:
        result = self._or()
        if self._peek() is not None:
            raise ValueError(f'unexpected {self._peek()!r}')
        return result

    def _or(self) -> tuple[str, str]:
        parts = [self._and()]
        while self._peek_word() == 'OR':
            self.pos += 1
            parts.append(self._and())
        if len(parts) == 1:
            return parts[0]
        category, reason = _best(parts)
        return category, f'OR expression, best option governs ({reason})'

    def _and(self) -> tuple[str, str]:
        parts = [self._factor()]
        while self._peek_word() == 'AND':
            self.pos += 1
            parts.append(self._factor())
        if len(parts) == 1:
            return parts[0]
        category, reason = _worst(parts)
        return category, f'AND expression, worst component governs ({reason})'

    def _factor(self) -> tuple[str, str]:
        if self._peek() == '(':
            self.pos += 1
            inner = self._or()
            if self._peek() != ')':
                raise ValueError('missing closing parenthesis')
            self.pos += 1
            return inner
        words: list[str] = []
        while (
            (tok := self._peek()) is not None
            and tok not in '()'
            and self._peek_word()
            not in (
                'AND',
                'OR',
                'WITH',
            )
        ):
            words.append(tok)
            self.pos += 1
        if not words:
            raise ValueError('empty licence term')
        if self._peek_word() == 'WITH':
            self.pos += 1
            if self._peek() is None or self._peek() in ('(', ')'):
                raise ValueError('WITH without an exception name')
            self.pos += 1
        verdict: tuple[str, str] = classify_license_string(' '.join(words))
        return verdict


def classify(expr: str | None) -> tuple[str, str]:
    """``(category, reason)`` of a licence string; never ``allow`` for empty or unparseable input."""
    if expr is None or not expr.strip():
        return 'unknown', 'no license metadata found'
    text = ' '.join(expr.split())
    low = text.lower()
    # Restricted riders are looked for in the RAW text, before any WITH clause is dropped:
    # "MIT WITH Commons-Clause" is not MIT.
    for sub in BLOCKER_SUBSTRINGS:
        if sub in low:
            return 'blocker', f"matches restricted term '{sub}'"
    if low in PLACEHOLDERS:
        return 'unknown', f'licence declared as the placeholder {expr.strip()!r}'
    if low in ALIASES:
        text = ALIASES[low]
    text = _OR_LATER.sub('or-later', text)
    try:
        return _Parser(text).parse()
    except ValueError as exc:
        return 'unknown', f'unparseable license expression ({exc}): {expr.strip()}'


# --- packages ------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Package:
    """One shipped package and the licence it declares."""

    eco: str
    area: str
    name: str
    version: str
    license: str
    source: str


#: How an allowlist line spells "the package declares no licence at all".
NO_LICENSE = '(none)'


def canonical_name(eco: str, name: str) -> str:
    """PEP 503 for Python (``Foo_Bar`` is ``foo-bar``), lower case for npm."""
    if eco == 'pip':
        return re.sub(r'[-_.]+', '-', name).lower()
    return name.lower()


def allow_key(eco: str, name: str, license_text: str) -> tuple[str, str, str]:
    """Allowlist key: the approval names the licence, so a relicense invalidates it."""
    return eco, canonical_name(eco, name), ' '.join(license_text.split()) or NO_LICENSE


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise InputError(f'{path}: cannot read as JSON ({exc})') from exc


def _license_value(value: object) -> str | None:
    """A ``license`` field that is a string, ``{"type": ...}`` or a list of either."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        return _license_value(value.get('type'))
    if isinstance(value, list):
        parts = [p for p in (_license_value(v) for v in value) if p]
        return ';'.join(parts) if parts else None
    return None


def _as_expression(value: str) -> str:
    """A ``;``-separated list is treated as AND, the conservative reading."""
    return ' AND '.join(p.strip() for p in value.split(';') if p.strip())


def _installed_license(lock_dir: Path, key: str) -> str | None:
    manifest = lock_dir / key / 'package.json'
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _license_value(data.get('license')) or _license_value(data.get('licenses'))


def load_npm_lock(path: Path, area: str) -> list[Package]:
    """Production entries of one ``package-lock.json`` (no ``dev: true``, no workspace links).

    The licence comes from the lock entry; when the lock carries none, from the installed
    ``node_modules/<pkg>/package.json`` next to it; else it stays empty and classifies unknown.
    """
    data = _read_json(path)
    entries = data.get('packages') if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise InputError(f"{path}: no 'packages' map (lockfileVersion 2 or 3 required)")
    packages: list[Package] = []
    for key, info in entries.items():
        if not key:
            continue  # the project itself
        if not isinstance(info, dict):
            raise InputError(f'{path}: entry {key!r} is not an object')
        if info.get('dev') is True or info.get('link') is True:
            continue
        name = info.get('name') or key.rsplit('node_modules/', 1)[-1]
        if not isinstance(name, str) or not name:
            raise InputError(f'{path}: cannot name entry {key!r}')
        found = _license_value(info.get('license'))
        source = 'lockfile'
        if found is None:
            found = _installed_license(path.parent, key)
            source = 'node_modules'
        if found is None:
            source = 'missing'
        packages.append(
            Package('npm', area, name, str(info.get('version', '')), found or '', source)
        )
    if not packages:
        raise InputError(f'{path}: zero production packages (empty input is a broken pipeline)')
    return packages


def _classifier_license(classifiers: object) -> str | None:
    """Names of the ``License :: ...`` trove classifiers, ``OR``-joined (dual licensing)."""
    if not isinstance(classifiers, list):
        return None
    names: set[str] = set()
    for item in classifiers:
        if not isinstance(item, str) or not item.startswith('License ::'):
            continue
        tail = [
            p.strip() for p in item.split('::')[1:] if p.strip() and p.strip() != 'OSI Approved'
        ]
        if tail:
            names.add(tail[-1])
    mapped = [ALIASES.get(n.lower(), n) for n in sorted(names)]
    return ' OR '.join(mapped) if mapped else None


def _body_label(text: str) -> str:
    """A short, stable stand-in for a licence BODY: first line plus a digest of the whole text.

    The allowlist key needs a string that changes when the text changes, not a page of prose.
    """
    first = text.strip().splitlines()[0].strip()[:60]
    return f'{first} [licence text sha256:{hashlib.sha256(text.encode()).hexdigest()[:12]}]'


def python_license(meta: Mapping[str, object]) -> tuple[str, str]:
    """``(licence string, source field)`` from one pip report ``metadata`` object.

    Order: PEP 639 ``license_expression``, free-text ``license``, ``License ::`` trove
    classifiers. The first source that classifies as something other than ``unknown`` wins, so
    a field that says nothing useful (``UNKNOWN``, a licence body) does not hide a clear
    classifier. A licence body that nothing else corroborates is labelled with a digest so an
    allowlist line approves that exact text.
    """
    candidates: list[tuple[str, str]] = []
    bodies: list[tuple[str, str]] = []
    for field in ('license_expression', 'license'):
        value = meta.get(field)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if '\n' in text or len(text) > _MAX_IDENTIFIER:
                bodies.append((_body_label(text), field))
            else:
                candidates.append((text, field))
    classified = _classifier_license(meta.get('classifier'))
    if classified:
        candidates.append((classified, 'classifier'))
    for text, field in candidates:
        if classify(text)[0] != 'unknown':
            return text, field
    if bodies:
        return bodies[0]
    if candidates:
        return candidates[0]
    return '', 'missing'


def load_pip_report(path: Path) -> list[Package]:
    """Every package one ``pip install --dry-run --report`` file would install."""
    data = _read_json(path)
    install = data.get('install') if isinstance(data, dict) else None
    if not isinstance(install, list) or not install:
        raise InputError(f"{path}: no 'install' list (is this a pip --report file?)")
    packages: list[Package] = []
    for item in install:
        meta = item.get('metadata') if isinstance(item, dict) else None
        if not isinstance(meta, dict) or not meta.get('name') or not meta.get('version'):
            raise InputError(f'{path}: install item without metadata name/version')
        text, source = python_license(meta)
        packages.append(
            Package('pip', 'backend-python', str(meta['name']), str(meta['version']), text, source)
        )
    return packages


# --- allowlist -----------------------------------------------------------------------------


def _reason_checker() -> Callable[[str], bool]:
    """The ``_is_real_reason`` primitive ``audit-tests.py`` owns, so the repo has one vocabulary."""
    path = Path(__file__).resolve().parents[1] / 'audit-tests.py'
    spec = importlib.util.spec_from_file_location('_license_gate_audit_tests', path)
    if spec is None or spec.loader is None:
        raise InputError(f'cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    checker: Callable[[str], bool] = module._is_real_reason
    return checker


def parse_allowlist(text: str, source: str = '<allowlist>') -> dict[tuple[str, str, str], str]:
    """Lines ``ecosystem:name<TAB>license<TAB>reason``; ``#`` starts a comment line."""
    is_real_reason = _reason_checker()
    entries: dict[tuple[str, str, str], str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        fields = raw.rstrip('\n').split('\t')
        if len(fields) != 3 or not all(f.strip() for f in fields[:2]):
            raise InputError(
                f'{source}:{number}: expected ecosystem:name<TAB>license<TAB>reason, got {raw!r}'
            )
        target, license_text, reason = (f.strip() for f in fields)
        eco, _, name = target.partition(':')
        if eco not in ('pip', 'npm') or not name:
            raise InputError(f'{source}:{number}: ecosystem must be pip or npm in {target!r}')
        if not is_real_reason(reason):
            raise InputError(f'{source}:{number}: entry {target!r} has no real reason ({reason!r})')
        key = allow_key(eco, name, license_text)
        if key in entries:
            raise InputError(f'{source}:{number}: duplicate entry for {target!r}')
        entries[key] = reason
    return entries


def load_allowlist(path: Path) -> dict[tuple[str, str, str], str]:
    try:
        return parse_allowlist(path.read_text(encoding='utf-8'), str(path))
    except OSError as exc:
        raise InputError(f'{path}: {exc}') from exc


# --- evaluation ----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Row:
    """A package with its verdict."""

    package: Package
    category: str
    reason: str
    allowlisted: str | None  # the written reason when an allowlist entry covers it


@dataclasses.dataclass
class Result:
    rows: list[Row]
    errors: list[str]


def evaluate(
    packages: list[Package],
    allowlist: Mapping[tuple[str, str, str], str],
    scanned: Iterable[str],
) -> Result:
    """Classify, apply the allowlist, and report violations plus stale allowlist lines.

    ``scanned`` lists the ecosystems this run looked at: an entry for an ecosystem that was
    not scanned cannot be judged stale (``--npm-only`` never sees the Python entries).
    """
    if not packages:
        raise InputError('zero packages to judge (empty input is a broken pipeline)')
    rows: list[Row] = []
    errors: list[str] = []
    matched: dict[tuple[str, str, str], bool] = {}
    for pkg in packages:
        category, reason = classify(_as_expression(pkg.license))
        key = allow_key(pkg.eco, pkg.name, pkg.license)
        approved = allowlist.get(key) if category in FAILING else None
        if key in allowlist and category in FAILING:
            matched[key] = True
        rows.append(Row(pkg, category, reason, approved))
        if category in FAILING and approved is None:
            shown = pkg.license or '(none)'
            errors.append(
                f'{category.upper()}: {pkg.eco}:{pkg.name} {pkg.version} declares {shown!r} '
                f'({reason}); replace it or add a reasoned allowlist line'
            )
    scanned_set = set(scanned)
    for key in allowlist:
        if key[0] in scanned_set and key not in matched:
            errors.append(
                f'STALE allowlist line {key[0]}:{key[1]} {key[2]!r}: no scanned package declares '
                'that licence and fails on it any more (removed, relicensed or now allowed)'
            )
    return Result(rows, errors)


# --- report version ------------------------------------------------------------------------

_VERSION = re.compile(r'\bv?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)\b')


def verify_report_version(summary: Path, version_file: Path) -> list[str]:
    """Errors when the report's ``Scope:`` image tags are not the ``VERSION`` file's version."""
    try:
        current = version_file.read_text(encoding='utf-8').strip().removeprefix('v')
        text = summary.read_text(encoding='utf-8')
    except OSError as exc:
        raise InputError(str(exc)) from exc
    if not _VERSION.fullmatch(current):
        raise InputError(f'{version_file}: {current!r} is not a version')
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith('Scope:')), None)
    if start is None:
        raise InputError(f"{summary}: no 'Scope:' line")
    block: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        block.append(line)
    stated = sorted(set(_VERSION.findall(' '.join(block))))
    if not stated:
        raise InputError(f'{summary}: the Scope paragraph names no version')
    if stated != [current]:
        return [
            f'licence report states {", ".join("v" + v for v in stated)} but VERSION is '
            f'v{current}: regenerate security-reports/license-compliance/ for this release'
        ]
    return []


# --- evidence ------------------------------------------------------------------------------


def _counts(rows: list[Row]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        area = f'{row.package.eco}/{row.package.area}'
        bucket = out.setdefault(area, dict.fromkeys(SEVERITY, 0))
        bucket[row.category] += 1
    return out


def render_json(result: Result, extra: Mapping[str, object]) -> str:
    rows = result.rows
    payload = {
        'status': 'fail' if result.errors else 'pass',
        'counts': _counts(rows),
        'errors': result.errors,
        'concerns': [_row_dict(r) for r in rows if r.category in ('concern', 'proprietary')],
        'allowlisted': [_row_dict(r) for r in rows if r.allowlisted is not None],
        'packages': [_row_dict(r) for r in rows],
        **extra,
    }
    return json.dumps(payload, indent=2, sort_keys=False) + '\n'


def _row_dict(row: Row) -> dict[str, object]:
    pkg = row.package
    return {
        'ecosystem': pkg.eco,
        'area': pkg.area,
        'name': pkg.name,
        'version': pkg.version,
        'license': pkg.license,
        'license_source': pkg.source,
        'class': CATEGORY_LABEL[row.category],
        'reason': row.reason,
        'allowlist_reason': row.allowlisted,
    }


def render_csv(result: Result) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(
        ['ecosystem', 'area', 'package', 'version', 'license', 'source', 'class', 'allowlisted']
    )
    for row in sorted(result.rows, key=lambda r: (r.package.eco, r.package.area, r.package.name)):
        pkg = row.package
        writer.writerow(
            [
                pkg.eco,
                pkg.area,
                pkg.name,
                pkg.version,
                pkg.license or 'UNKNOWN',
                pkg.source,
                CATEGORY_LABEL[row.category],
                'yes' if row.allowlisted is not None else 'no',
            ]
        )
    return buf.getvalue()


def _md_cell(text: str) -> str:
    return text.replace('|', '/').replace('\n', ' ')


def render_markdown(result: Result, notes: list[str]) -> str:
    rows = result.rows
    lines = [
        '# Dependency licence gate',
        '',
        'Policy: permissive-only (`scripts/lib/license_policy.py`). Declared metadata only: it does',
        'not read licence texts, detect vendored code, or judge OS packages (the image SBOM at',
        'release does). Unknown or missing licences fail unless allowlisted with a reason.',
        '',
        f'Result: **{"FAIL" if result.errors else "PASS"}**',
        '',
        '| area | allow | concern | proprietary | unknown | blocker |',
        '|---|---|---|---|---|---|',
    ]
    for area, c in sorted(_counts(rows).items()):
        lines.append(
            f'| {area} | {c["allow"]} | {c["concern"]} | {c["proprietary"]} | '
            f'{c["unknown"]} | {c["blocker"]} |'
        )
    lines.append('')
    lines += [f'- {n}' for n in notes]
    if notes:
        lines.append('')
    if result.errors:
        lines += ['## Violations', '', *[f'- {e}' for e in result.errors], '']
    exceptions = [r for r in rows if r.allowlisted is not None]
    if exceptions:
        lines += [
            '## Allowlisted exceptions',
            '',
            '| package | version | license | class | reason |',
            '|---|---|---|---|---|',
        ]
        lines += [
            f'| {r.package.eco}:{r.package.name} | {r.package.version} | '
            f'{_md_cell(r.package.license or "UNKNOWN")} | {CATEGORY_LABEL[r.category]} | '
            f'{_md_cell(r.allowlisted or "")} |'
            for r in exceptions
        ]
        lines.append('')
    concerns = [r for r in rows if r.category in ('concern', 'proprietary')]
    if concerns:
        lines += [
            '## Concerns (listed, not failing)',
            '',
            '| package | version | license | class |',
            '|---|---|---|---|',
        ]
        lines += [
            f'| {r.package.eco}:{r.package.name} | {r.package.version} | '
            f'{_md_cell(r.package.license)} | {CATEGORY_LABEL[r.category]} |'
            for r in concerns
        ]
        lines.append('')
    return '\n'.join(lines)
