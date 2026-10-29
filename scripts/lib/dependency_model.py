"""Shared types, git/worktree sources and version rules for the dependency inventory, stdlib only.

``Dep`` is one dependency as a manifest states it, ``Manifest`` all deps read from one file,
``Inventory`` everything found at one ref. ``Source`` is where files come from (a git ref or the
working tree). ``ManifestError`` is how every parser fails closed: a manifest that exists and
cannot be parsed must never come back as an empty one.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_VERSION_RE = re.compile(r'\d+(?:\.\d+)*')


class ManifestError(ValueError):
    """A manifest could not be read or parsed; the caller must not treat it as empty."""


@dataclass(frozen=True)
class Dep:
    """One dependency as a manifest states it."""

    name: str
    version: str
    versions: tuple[str, ...] = ()  # strings the MAJOR rule compares; empty when not numeric
    scope: str = ''
    direct: bool | None = True  # None: not applicable (images, includes)
    license: str | None = None
    source: str = ''  # where it comes from: extras, tarball URL, integrity, alias target


@dataclass
class Manifest:
    """All dependencies read from one file."""

    ecosystem: str
    path: str
    kind: str  # manifest | lock | declared | image
    deps: dict[str, Dep] = field(default_factory=dict)


@dataclass
class Inventory:
    """Everything ``collect`` found at one ref."""

    manifests: dict[str, Manifest] = field(default_factory=dict)
    unsupported: dict[str, str] = field(default_factory=dict)  # path -> content


class Source:
    """Where manifests are read from: a git ref or the working tree."""

    def paths(self) -> list[str]:
        raise NotImplementedError

    def read(self, path: str) -> str:
        raise NotImplementedError


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ['git', *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=False,
    )
    if proc.returncode != 0:
        raise ManifestError(f'git {" ".join(args)}: {proc.stderr.strip()}')
    return proc.stdout


class RefSource(Source):
    """Files as committed at ``ref``, read with ``git show`` (nothing is checked out)."""

    def __init__(self, repo: Path, ref: str) -> None:
        self.repo, self.ref = repo, ref

    def paths(self) -> list[str]:
        return _git(self.repo, 'ls-tree', '-r', '--name-only', self.ref).splitlines()

    def read(self, path: str) -> str:
        return _git(self.repo, 'show', f'{self.ref}:{path}')


class WorktreeSource(Source):
    """Files on disk: tracked plus untracked-but-not-ignored, so an uncommitted edit counts."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def paths(self) -> list[str]:
        out = _git(self.repo, 'ls-files', '--cached', '--others', '--exclude-standard')
        return [p for p in out.splitlines() if (self.repo / p).is_file()]

    def read(self, path: str) -> str:
        try:
            return (self.repo / path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            raise ManifestError(f'{path}: {exc}') from exc


def parse_version(text: str) -> tuple[int, ...] | None:
    """The first dotted number in ``text`` (``>=1.2``, ``^0.3.1``, ``bin-2.9.7``), else ``None``."""
    found = _VERSION_RE.search(text)
    return tuple(int(p) for p in found[0].split('.')) if found else None


def is_major(old: tuple[str, ...], new: tuple[str, ...]) -> bool:
    """A major bump, or a minor bump while the major is still 0 (below 1.0 anything may break).

    Compares the highest version on each side (an npm package can be installed at several).
    """
    a = max((v for s in old if (v := parse_version(s))), default=None)
    b = max((v for s in new if (v := parse_version(s))), default=None)
    if a is None or b is None:
        return False
    if a[0] != b[0]:
        return True
    return a[0] == 0 and a[1:2] != b[1:2]


def logical_lines(text: str) -> list[str]:
    """Lines with backslash continuations joined."""
    return text.replace('\r\n', '\n').replace('\\\n', ' ').splitlines()


def strip_comment(line: str) -> str:
    """``line`` without a trailing ``# comment`` (a ``#`` glued to a word is not a comment)."""
    return re.split(r'(?:^|\s)#', line, maxsplit=1)[0].strip()
