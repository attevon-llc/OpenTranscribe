"""Read every dependency manifest of a repo at one git ref (or the working tree), stdlib only.

``collect`` turns a ``Source`` into an ``Inventory``: one ``Manifest`` per file. Covered:
``requirements*.txt`` (and anything they pull in with ``-r`` / ``-c``), ``package.json`` +
``package-lock.json`` pairs, and (via ``dependency_images``) the ``FROM`` lines of Dockerfiles and
the ``image:`` lines of compose files: a base image is a dependency like any other.

Fail closed: a manifest that exists and cannot be parsed raises ``ManifestError``. An empty
manifest diffed against a full one is a false "everything was removed"; an empty one diffed
against an empty one is a false "no changes".
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping

from dependency_images import parse_compose, parse_dockerfile
from dependency_model import (
    Dep,
    Inventory,
    Manifest,
    ManifestError,
    Source,
    logical_lines,
    parse_version,
    strip_comment,
)
from requirements_resolve import normalise

NPM_LOCKS = ('package-lock.json', 'npm-shrinkwrap.json')
#: Lockfiles of ecosystems this tool does not read. A change to one is reported, never ignored.
UNSUPPORTED_LEAVES = (
    'yarn.lock',
    'pnpm-lock.yaml',
    'bun.lock',
    'bun.lockb',
    'uv.lock',
    'poetry.lock',
    'Pipfile.lock',
)
REQ_RE = re.compile(r'(^|/)requirements[^/]*\.txt$')
DOCKERFILE_RE = re.compile(r'^(Dockerfile(\..+)?|.+\.Dockerfile)$')
COMPOSE_RE = re.compile(r'^(docker-)?compose[^/]*\.ya?ml$')
NPM_SECTIONS = {
    'dependencies': 'prod',
    'optionalDependencies': 'prod',
    'devDependencies': 'dev',
}
_INCLUDE_RE = re.compile(r'^(?:-r|--requirement|-c|--constraint)[\s=]+(\S+)$')
_REQ_RE = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$')


# --- python ------------------------------------------------------------------------------------


def parse_requirements(text: str, role: str = '') -> dict[str, Dep]:
    """One requirements file -> deps keyed ``name;marker`` (options and includes keyed by text).

    ``role`` is how the file is installed (``--no-deps``): it is the file's scope. A duplicate
    key (same name, same marker) joins its versions rather than letting the last line win.
    """
    out: dict[str, Dep] = {}
    for raw in logical_lines(text):
        line = re.sub(r'\s--hash(?:=|\s+)\S+', '', strip_comment(raw)).strip()
        if not line:
            continue
        key, dep = _parse_requirement(line, role)
        old = out.get(key)
        if old is not None and old.version != dep.version:
            dep = Dep(
                dep.name,
                f'{old.version} | {dep.version}',
                old.versions + dep.versions,
                dep.scope,
                dep.direct,
                None,
                dep.source,
            )
        out[key] = dep
    return out


def _parse_requirement(line: str, role: str) -> tuple[str, Dep]:
    include = _INCLUDE_RE.match(line)
    if include:
        return f'include {include[1]}', Dep(f'-r {include[1]}', 'include', direct=None)
    editable = re.match(r'^(?:-e|--editable)[\s=]+(.+)$', line)
    if line.startswith('-') and not editable:
        return line, Dep(line, 'option', scope=role, direct=None)
    spec, _, marker = (editable[1] if editable else line).partition(';')
    spec, marker = spec.strip(), re.sub(r'\s+', '', marker)
    suffix = f';{marker}' if marker else ''
    url_part = spec.split('#', 1)[0]
    if editable or '://' in url_part or url_part.startswith(('./', '../', '/')):
        at = re.match(r'([A-Za-z0-9][A-Za-z0-9._-]*)\s*@\s*(\S+)$', url_part)
        egg = re.search(r'[#&]egg=([A-Za-z0-9._-]+)', spec)
        if at:
            name, ref = at[1], at[2]
        else:
            name = egg[1] if egg else posixpath.basename(url_part.split('@')[0].rstrip('/'))
            ref = url_part
        return normalise(name) + suffix, Dep(name, ref, scope=role, source='url')
    match = _REQ_RE.fullmatch(spec)
    if not match:
        raise ManifestError(f'unparsable requirement line: {line!r}')
    version = match[3].replace(' ', '') or '(any)'
    if re.fullmatch(r'==[^,=<>!~]+', version):
        version = version[2:]
    return normalise(match[1]) + suffix, Dep(
        match[1],
        version,
        (version,) if parse_version(version) else (),
        role,
        source=match[2] or '',
    )


def requirement_includes(path: str, text: str) -> list[str]:
    """Repo paths a requirements file pulls in with ``-r`` / ``-c``."""
    found = []
    for raw in logical_lines(text):
        include = _INCLUDE_RE.match(strip_comment(raw))
        if include:
            if '://' in include[1]:
                raise ManifestError(f'{path}: cannot follow a URL include {include[1]!r}')
            found.append(posixpath.normpath(posixpath.join(posixpath.dirname(path), include[1])))
    return found


# --- npm ---------------------------------------------------------------------------------------


def _json_object(path: str, text: str) -> dict[str, object]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f'{path}: not valid JSON ({exc})') from exc
    if not isinstance(data, dict):
        raise ManifestError(f'{path}: top level is not a JSON object')
    return data


def parse_package_json(path: str, text: str) -> dict[str, Dep]:
    """Declared dependencies of a ``package.json`` (ranges, as written)."""
    data = _json_object(path, text)
    out: dict[str, Dep] = {}
    for section, scope in NPM_SECTIONS.items():
        table = data.get(section, {})
        if not isinstance(table, dict) or not all(isinstance(v, str) for v in table.values()):
            raise ManifestError(f'{path}: "{section}" is not a name -> range map')
        for name, spec in table.items():
            out.setdefault(name, Dep(name, spec, (spec,), scope))
    return out


def _lock_license(info: Mapping[str, object]) -> str | None:
    value = info.get('license')
    if isinstance(value, dict):
        value = value.get('type')
    if isinstance(value, list):
        value = ' OR '.join(str(v) for v in value)
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_npm_lock(path: str, text: str, direct: set[str] | None = None) -> dict[str, Dep]:
    """``package-lock.json`` (v1/v2/v3) -> one Dep per package name.

    ``direct``: names declared in the directory's ``package.json``; ``None`` falls back to the
    lock's own root entry. A name installed at several versions is one Dep with all of them.
    ``scope`` is ``dev`` only when every installed copy carries the lock's ``dev`` flag.
    """
    data = _json_object(path, text)
    seen: dict[str, list[tuple[str, bool, str, str | None]]] = {}

    def add(name: str, info: Mapping[str, object], alias: object = None) -> None:
        source = f'{alias or ""}|{info.get("resolved", "")}|{info.get("integrity", "")}'
        seen.setdefault(name, []).append(
            (str(info.get('version', '')), info.get('dev') is True, source, _lock_license(info))
        )

    packages = data.get('packages')
    if isinstance(packages, dict):
        root = packages.get('', {})
        if direct is None:
            direct = {n for s in NPM_SECTIONS for n in (root.get(s) or {})}
        for key, info in packages.items():
            if not isinstance(info, dict):
                raise ManifestError(f'{path}: packages[{key!r}] is not an object')
            if 'node_modules/' not in key or info.get('link') or 'version' not in info:
                continue
            name = key.rsplit('node_modules/', 1)[-1]
            alias = info.get('name') if info.get('name') not in (None, name) else None
            add(name, info, alias)
    elif isinstance(top := data.get('dependencies'), dict):  # lockfile v1: no root, no packages
        direct = direct if direct is not None else set(top)

        def walk(deps: dict[str, object]) -> None:
            for name, info in deps.items():
                if not isinstance(info, dict):
                    raise ManifestError(f'{path}: dependencies[{name!r}] is not an object')
                add(name, info)
                nested = info.get('dependencies')
                if isinstance(nested, dict):
                    walk(nested)

        walk(top)
    else:
        raise ManifestError(f'{path}: neither "packages" nor "dependencies" in the lockfile')
    return {name: _npm_dep(name, rows, name in (direct or set())) for name, rows in seen.items()}


def _npm_dep(name: str, rows: list[tuple[str, bool, str, str | None]], direct: bool) -> Dep:
    versions = tuple(sorted({v for v, _, _, _ in rows}))
    licenses = sorted({lic for _, _, _, lic in rows if lic})
    return Dep(
        name,
        ', '.join(versions),
        versions,
        'dev' if all(dev for _, dev, _, _ in rows) else 'prod',
        direct,
        ' AND '.join(licenses) or None,
        ';'.join(sorted(f'{v}{s}' for v, _, s, _ in rows)),
    )


# --- collection --------------------------------------------------------------------------------


def _usable(path: str) -> bool:
    return 'node_modules/' not in path and '/venv/' not in f'/{path}'


def _collect_python(
    source: Source, paths: set[str], roles: Mapping[str, tuple[str, ...]]
) -> dict[str, Manifest]:
    out: dict[str, Manifest] = {}
    todo = sorted(p for p in paths if REQ_RE.search(p))
    while todo:
        path = todo.pop()
        if path in out:
            continue
        text = source.read(path)
        role = ' '.join(roles.get(path, ()))
        out[path] = Manifest('python', path, 'manifest', parse_requirements(text, role))
        for included in requirement_includes(path, text):
            if included not in paths:
                raise ManifestError(f'{path}: includes {included}, which is not in this tree')
            todo.append(included)
    return out


def _collect_npm(source: Source, paths: set[str]) -> dict[str, Manifest]:
    out: dict[str, Manifest] = {}
    for path in sorted(paths):
        leaf = posixpath.basename(path)
        directory = posixpath.dirname(path)
        if leaf == 'package.json':
            text = source.read(path)
            out[path] = Manifest('npm', path, 'declared', parse_package_json(path, text))
        elif leaf in NPM_LOCKS:
            manifest = posixpath.join(directory, 'package.json')
            declared = (
                set(parse_package_json(manifest, source.read(manifest)))
                if manifest in paths
                else None
            )
            deps = parse_npm_lock(path, source.read(path), declared)
            out[path] = Manifest('npm', path, 'lock', deps)
    return out


def _collect_images(source: Source, paths: set[str]) -> dict[str, Manifest]:
    out: dict[str, Manifest] = {}
    for path in sorted(paths):
        leaf = posixpath.basename(path)
        if DOCKERFILE_RE.match(leaf):
            out[path] = Manifest('docker', path, 'image', parse_dockerfile(path, source.read(path)))
        elif COMPOSE_RE.match(leaf):
            out[path] = Manifest('docker', path, 'image', parse_compose(path, source.read(path)))
    return out


def collect(source: Source, roles: Mapping[str, tuple[str, ...]] | None = None) -> Inventory:
    """Read every supported manifest ``source`` holds.

    ``roles`` maps a requirements path to its install role (``backend/requirements-nodeps.txt``
    -> ``('--no-deps',)``).
    """
    paths = {p for p in source.paths() if _usable(p)}
    inv = Inventory()
    inv.manifests.update(_collect_python(source, paths, roles or {}))
    inv.manifests.update(_collect_npm(source, paths))
    inv.manifests.update(_collect_images(source, paths))
    for path in sorted(paths):
        if posixpath.basename(path) in UNSUPPORTED_LEAVES:
            inv.unsupported[path] = source.read(path)
    return inv
