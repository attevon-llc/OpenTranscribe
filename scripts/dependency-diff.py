#!/usr/bin/env python3
"""Package-change check: what dependencies does this change add, remove or move?

Evidence, not a gate: the exit code never depends on what changed. It reads each manifest at
two git refs (or the working tree) with ``git show`` and writes ``dependency-diff.md`` (for the CI
job summary) and ``dependency-diff.json`` (for tooling) into ``--out-dir``.

Covered, per ecosystem:

* python: every tracked ``requirements*.txt`` (and anything they pull in with ``-r`` / ``-c``):
  markers kept (``name;marker`` is its own entry), ``--extra-index-url`` and other options, local
  versions such as ``+cu128``, git/URL pins, and the ``--no-deps`` role of
  ``requirements-nodeps.txt``. With ``--resolved-base`` / ``--resolved-head`` (directories of the
  pip reports ``check-requirements-resolve.py --report-dir`` writes) the transitive closure is
  diffed too: direct = pinned in a requirements file, transitive = only in the pip report.
* npm: each ``package.json`` (declared ranges) and ``package-lock.json`` (lockfile v1/v2/v3).
  Direct = a key in that directory's ``package.json``; prod vs dev from the lockfile ``dev`` flag.
* docker: the external ``FROM`` images of every Dockerfile and the ``image:`` of every compose
  file. A base image is a dependency; a digest-only change is a change.

Flags: ``MAJOR`` (major change, or a minor change while the major is 0), ``LICENSE-CHANGED``
(both sides state a licence and they differ), ``NEW`` with the licence class from
``scripts/lib/license_policy.py`` when a licence is known.

Fail closed: a manifest that exists but cannot be parsed, a ref git cannot resolve, or a
half-given resolved pair exits 2. An unreadable manifest is never an empty diff. A changed
lockfile this tool cannot read (yarn, pnpm, uv, poetry ...) is listed, and "No dependency changes"
is never printed beside it.

Usage::

    python3 scripts/dependency-diff.py --base origin/master --head HEAD --out-dir out
    python3 scripts/dependency-diff.py --base HEAD --head WORKTREE --out-dir out

Exit codes: 0 report written, 2 unusable input. It does not say whether a change is safe.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import posixpath
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from dependency_inventory import collect
from dependency_model import (
    Dep,
    Inventory,
    Manifest,
    ManifestError,
    RefSource,
    Source,
    WorktreeSource,
    is_major,
)
from license_policy import CATEGORY_LABEL, classify_license_string
from requirements_resolve import STEPS, ResolvedPackage, normalise, resolved_packages

WORKTREE = 'WORKTREE'
ECOSYSTEMS = ('python', 'npm', 'docker')


@dataclasses.dataclass
class Row:
    """One added, removed or changed dependency."""

    name: str
    change: str  # added | removed | changed
    old: str | None
    new: str | None
    scope: str
    direct: bool | None
    flags: list[str]
    license: str | None = None
    license_class: str | None = None
    notes: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Section:
    """The rows for one file (``kind`` says how it was read)."""

    ecosystem: str
    path: str
    kind: str
    rows: list[Row] = dataclasses.field(default_factory=list)


def license_class(text: str | None) -> str | None:
    """The policy's label for a licence string, ``None`` when no licence is known."""
    if not text:
        return None
    label: str = CATEGORY_LABEL[classify_license_string(text)[0]]
    return label


def diff_deps(old: dict[str, Dep], new: dict[str, Dep]) -> list[Row]:
    """Rows for the difference between two key -> Dep maps (added, removed, then changed)."""
    rows: list[Row] = []
    for key in sorted(new.keys() - old.keys()):
        dep = new[key]
        rows.append(
            Row(
                dep.name,
                'added',
                None,
                dep.version,
                dep.scope,
                dep.direct,
                ['NEW'],
                dep.license,
                license_class(dep.license),
            )
        )
    for key in sorted(old.keys() - new.keys()):
        dep = old[key]
        rows.append(
            Row(dep.name, 'removed', dep.version, None, dep.scope, dep.direct, [], dep.license)
        )
    for key in sorted(old.keys() & new.keys()):
        row = _changed(old[key], new[key])
        if row is not None:
            rows.append(row)
    return rows


def _changed(a: Dep, b: Dep) -> Row | None:
    notes = []
    if a.version != b.version:
        notes.append('version')
    elif a.source != b.source:
        notes.append('same version, source changed')
    if a.scope != b.scope:
        notes.append(f'scope {a.scope or "-"} -> {b.scope or "-"}')
    if a.direct != b.direct:
        notes.append('direct <-> transitive')
    flags = []
    if is_major(a.versions, b.versions):
        flags.append('MAJOR')
    if a.license and b.license and a.license != b.license:
        flags.append('LICENSE-CHANGED')
        notes.append(f'licence {a.license} -> {b.license}')
    if not notes:
        return None
    return Row(
        b.name,
        'changed',
        a.version,
        b.version,
        b.scope,
        b.direct,
        flags,
        b.license,
        license_class(b.license),
        notes,
    )


def direct_names(inv: Inventory, path: str, seen: set[str] | None = None) -> set[str]:
    """Normalised names a requirements file pins itself or through its ``-r`` / ``-c`` includes."""
    seen = seen if seen is not None else set()
    manifest = inv.manifests.get(path)
    if manifest is None or path in seen:
        return set()
    seen.add(path)
    names = set()
    for dep in manifest.deps.values():
        if dep.version == 'include':
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), dep.name[3:]))
            names |= direct_names(inv, target, seen)
        elif dep.direct:
            names.add(normalise(dep.name))
    return names


def _load_reports(directory: Path | None) -> dict[str, dict[str, ResolvedPackage]]:
    """``<requirements file>.json`` reports in ``directory`` -> resolved packages."""
    if directory is None:
        return {}
    if not directory.is_dir():
        raise ManifestError(f'{directory}: not a directory')
    found = {}
    for report in sorted(directory.glob('*.json')):
        try:
            data = json.loads(report.read_text(encoding='utf-8'))
            found[report.name.removesuffix('.json')] = resolved_packages(data)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise ManifestError(f'{report}: unusable pip report ({exc})') from exc
    return found


def _with_licenses(manifest: Manifest, packages: dict[str, ResolvedPackage]) -> Manifest:
    """The manifest's deps with the licence the pip report states for the same package."""
    deps = {}
    for key, dep in manifest.deps.items():
        found = packages.get(normalise(dep.name))
        deps[key] = dataclasses.replace(dep, license=found.license) if found else dep
    return Manifest(manifest.ecosystem, manifest.path, manifest.kind, deps)


def _transitive(
    inv: Inventory, path: str, packages: dict[str, ResolvedPackage], role: str
) -> dict[str, Dep]:
    direct = direct_names(inv, path)
    return {
        name: Dep(name, pkg.version, (pkg.version,), role, False, pkg.license)
        for name, pkg in packages.items()
        if name not in direct
    }


def compare(
    old: Inventory,
    new: Inventory,
    old_reports: dict[str, dict[str, ResolvedPackage]],
    new_reports: dict[str, dict[str, ResolvedPackage]],
) -> tuple[list[Section], list[str]]:
    """Sections (one per changed file) plus notes on what was not compared."""
    old_m, new_m = dict(old.manifests), dict(new.manifests)
    resolved_files = {}
    for path, manifest in {**old_m, **new_m}.items():
        report = posixpath.basename(path)
        if manifest.ecosystem == 'python' and report in old_reports and report in new_reports:
            resolved_files[path] = report
    for path, report in resolved_files.items():
        if path in old_m:
            old_m[path] = _with_licenses(old_m[path], old_reports[report])
        if path in new_m:
            new_m[path] = _with_licenses(new_m[path], new_reports[report])
    sections = []
    for path in sorted(old_m.keys() | new_m.keys()):
        a, b = old_m.get(path), new_m.get(path)
        shape = b or a
        rows = diff_deps(a.deps if a else {}, b.deps if b else {})
        if shape and rows:
            sections.append(Section(shape.ecosystem, path, shape.kind, rows))
    notes = []
    for path, report in sorted(resolved_files.items()):
        role = ' '.join(dict(STEPS).get(report, ()))
        rows = diff_deps(
            _transitive(old, path, old_reports[report], role),
            _transitive(new, path, new_reports[report], role),
        )
        if rows:
            sections.append(Section('python', path, 'resolved', rows))
    python_paths = {p for p, m in {**old_m, **new_m}.items() if m.ecosystem == 'python'}
    skipped = sorted(p for p in python_paths if p not in resolved_files)
    if skipped and (old_reports or new_reports):
        notes.append(
            'Transitive Python closure NOT compared for: '
            + ', '.join(skipped)
            + ' (no pip report for the file at both refs).'
        )
    return sections, notes


# --- rendering ---------------------------------------------------------------------------------


def _cell(text: str | None) -> str:
    return (text or '').replace('|', '\\|').replace('\n', ' ')


def _flag_text(row: Row) -> str:
    parts = []
    for flag in row.flags:
        if flag == 'NEW':
            parts.append(f'**NEW** ({row.license_class})' if row.license_class else '**NEW**')
        else:
            parts.append(f'**{flag}**')
    return ' '.join(parts)


def _kind_text(row: Row) -> str:
    kinds = {True: 'direct', False: 'transitive', None: ''}
    return ', '.join(x for x in (kinds[row.direct], row.scope) if x)


def render_md(
    base: str, head: str, sections: list[Section], notes: list[str], unsupported: list[str]
) -> str:
    """The markdown report. "No dependency changes" is printed only when that is provably so."""
    lines = [f'# Dependency changes (`{base}` -> `{head}`)', '']
    if not sections and not unsupported:
        lines.append('No dependency changes.')
    for eco in ECOSYSTEMS:
        mine = [s for s in sections if s.ecosystem == eco]
        if not mine:
            continue
        lines += [f'## {eco}', '']
        for sec in mine:
            counts = {
                c: sum(r.change == c for r in sec.rows) for c in ('added', 'removed', 'changed')
            }
            lines += [
                f'### `{sec.path}` ({sec.kind})',
                '',
                f'{counts["added"]} added, {counts["removed"]} removed, {counts["changed"]} changed',
                '',
                '| change | package | from | to | kind | flags | notes |',
                '|---|---|---|---|---|---|---|',
            ]
            for r in sec.rows:
                lines.append(
                    f'| {r.change} | {_cell(r.name)} | {_cell(r.old)} | {_cell(r.new)} '
                    f'| {_cell(_kind_text(r))} | {_cell(_flag_text(r))} | {_cell("; ".join(r.notes))} |'
                )
            lines.append('')
    if unsupported:
        lines += ['## NOT DIFFED (lockfile format this tool cannot read)', '']
        lines += [f'- `{p}` changed. Review it with dependency-review.' for p in unsupported]
        lines.append('')
    if notes:
        lines += ['## Not compared', ''] + [f'- {n}' for n in notes] + ['']
    return '\n'.join(lines) + '\n'


def summarise(sections: list[Section]) -> dict[str, object]:
    """Counts for the JSON file and the log line."""
    per_eco = {
        eco: {
            c: sum(r.change == c for s in sections if s.ecosystem == eco for r in s.rows)
            for c in ('added', 'removed', 'changed')
        }
        for eco in ECOSYSTEMS
    }
    flags = {
        f: sum(f in r.flags for s in sections for r in s.rows)
        for f in ('MAJOR', 'LICENSE-CHANGED', 'NEW')
    }
    return {'ecosystems': per_eco, 'flags': flags}


# --- main --------------------------------------------------------------------------------------


def _source(repo: Path, ref: str) -> Source:
    return WorktreeSource(repo) if ref == WORKTREE else RefSource(repo, ref)


def _unsupported(old: Inventory, new: Inventory) -> list[str]:
    paths = old.unsupported.keys() | new.unsupported.keys()
    return sorted(p for p in paths if old.unsupported.get(p) != new.unsupported.get(p))


def run(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    """Compute the report; raises ``ManifestError`` on any unusable input."""
    if bool(args.resolved_base) != bool(args.resolved_head):
        raise ManifestError('--resolved-base and --resolved-head must be given together')
    roles = {f'backend/{name}': extra for name, extra in STEPS if extra}
    old = collect(_source(args.repo, args.base), roles)
    new = collect(_source(args.repo, args.head), roles)
    sections, notes = compare(
        old, new, _load_reports(args.resolved_base), _load_reports(args.resolved_head)
    )
    if not (args.resolved_base and args.resolved_head):
        notes = ['Transitive Python closure not compared (no --resolved-base/--resolved-head).']
    unsupported = _unsupported(old, new)
    result = {
        'base': args.base,
        'head': args.head,
        'summary': summarise(sections),
        'sections': [dataclasses.asdict(s) for s in sections],
        'notes': notes,
        'unsupported': unsupported,
    }
    return render_md(args.base, args.head, sections, notes, unsupported), result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--repo', type=Path, default=Path.cwd(), help='git checkout to read')
    parser.add_argument('--base', required=True, help='base ref')
    parser.add_argument('--head', default='HEAD', help=f'head ref, or {WORKTREE} for the disk')
    parser.add_argument('--out-dir', type=Path, default=Path('dependency-diff'))
    parser.add_argument('--resolved-base', type=Path, help='pip reports for the base ref')
    parser.add_argument('--resolved-head', type=Path, help='pip reports for the head ref')
    args = parser.parse_args(argv)
    try:
        markdown, result = run(args)
    except (ManifestError, OSError) as exc:
        sys.stderr.write(f'ERROR: {exc}\n')
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / 'dependency-diff.md').write_text(markdown, encoding='utf-8')
    (args.out_dir / 'dependency-diff.json').write_text(
        json.dumps(result, indent=2) + '\n', encoding='utf-8'
    )
    sys.stdout.write(markdown)
    return 0


if __name__ == '__main__':
    sys.exit(main())
