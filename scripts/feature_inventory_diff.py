"""Diff and accept-file logic for the feature inventory (see feature_inventory.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class AcceptError(ValueError):
    """The accept file is malformed (e.g. an entry without a reason)."""


@dataclass(frozen=True)
class Difference:
    section: str
    key: str
    kind: str  # added | removed | changed
    old: object = None
    new: object = None


def load_accept(path: str | Path | None) -> list[tuple[str, str, str]]:
    """Parse ``section<TAB>key<TAB>reason`` lines; the reason is mandatory.

    A key ending in ``*`` is a prefix match. Blank lines and ``#`` comments are skipped.
    """
    entries = []
    if path is None:
        return entries
    for n, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) < 3 or not parts[0].strip() or not parts[1].strip() or not parts[2].strip():
            raise AcceptError(
                f'{path}:{n}: expected section<TAB>key<TAB>reason with a non-empty reason'
            )
        entries.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return entries


def is_accepted(d: Difference, accept: list[tuple[str, str, str]]) -> str | None:
    for section, key, reason in accept:
        if section != d.section:
            continue
        if key == d.key or (key.endswith('*') and d.key.startswith(key[:-1])):
            return reason
    return None


def compute_diff(a: dict, b: dict) -> list[Difference]:
    out = []
    for section in sorted(set(a) | set(b)):
        sa, sb = a.get(section), b.get(section)
        if sa is None or sb is None:
            out.append(Difference(section, '<section>', 'removed' if sb is None else 'added'))
            continue
        for key in sorted(set(sa) | set(sb)):
            if key not in sb:
                out.append(Difference(section, key, 'removed', sa[key]))
            elif key not in sa:
                out.append(Difference(section, key, 'added', None, sb[key]))
            elif sa[key] != sb[key]:
                out.append(Difference(section, key, 'changed', sa[key], sb[key]))
    return out


def _short(v, limit=160) -> str:
    s = json.dumps(v, sort_keys=True, default=repr)
    return s if len(s) <= limit else s[: limit - 3] + '...'


def _change(old, new, limit=110) -> str:
    """Describe WHERE two values differ, not just that they do (truncated heads look identical)."""
    if isinstance(old, dict) and isinstance(new, dict):
        keys = [k for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)]
        return '; '.join(f'{k}: {_change(old.get(k), new.get(k), limit)}' for k in keys[:6])
    if isinstance(old, str) and isinstance(new, str):
        i = next(
            (n for n, (x, y) in enumerate(zip(old, new, strict=False)) if x != y),
            min(len(old), len(new)),
        )
        lo = max(0, i - 20)
        return f'...{old[lo : i + 30]!r} -> ...{new[lo : i + 30]!r}'
    return f'{_short(old, limit)} -> {_short(new, limit)}'


def render(diffs: list[Difference], accept: list[tuple[str, str, str]]) -> tuple[str, int]:
    """Return (report text, number of UNACCEPTED differences)."""
    lines, unaccepted = [], 0
    by_section: dict[str, list[Difference]] = {}
    for d in diffs:
        by_section.setdefault(d.section, []).append(d)
    lines.append('SUMMARY (added/removed/changed per section)')
    for s, ds in by_section.items():
        c = {k: sum(1 for d in ds if d.kind == k) for k in ('added', 'removed', 'changed')}
        lines.append(f'  {s}: +{c["added"]} -{c["removed"]} ~{c["changed"]}')
    if not diffs:
        lines.append('  (no differences)')
    for s, ds in by_section.items():
        lines.append(f'\n== {s} ==')
        for d in ds:
            reason = is_accepted(d, accept)
            if reason is None:
                unaccepted += 1
            tag = f'  [accepted: {reason}]' if reason else ''
            sym = {'added': '+', 'removed': '-', 'changed': '~'}[d.kind]
            detail = f'  {_change(d.old, d.new)}' if d.kind == 'changed' else ''
            lines.append(f'{sym} {d.key}{detail}{tag}')
    lines.append(f'\nTOTAL differences: {len(diffs)}, unaccepted: {unaccepted}')
    return '\n'.join(lines) + '\n', unaccepted
