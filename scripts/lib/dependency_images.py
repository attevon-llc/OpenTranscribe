"""Container images as dependencies: Dockerfile ``FROM`` lines and compose ``image:`` lines."""

from __future__ import annotations

import re
from collections.abc import Mapping

from dependency_model import Dep, ManifestError, logical_lines, strip_comment


def image_tag_versions(ref: str) -> tuple[str, ...]:
    """The tag of an image reference (``node:26-alpine`` -> ``26-alpine``); digests are not versions."""
    if '$' in ref:
        return ()
    name = ref.split('@', 1)[0].rsplit('/', 1)[-1]
    return (name.split(':', 1)[1],) if ':' in name else ()


def _expand_args(ref: str, args: Mapping[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        name, default = match[1] or match[3], match[2]
        return args.get(name, default if default is not None else match[0])

    return re.sub(r'\$(?:\{(\w+)(?::?-([^}]*))?\}|(\w+))', sub, ref)


def parse_dockerfile(path: str, text: str) -> dict[str, Dep]:
    """External base images of a Dockerfile, keyed by stage alias (else image name + ordinal).

    A ``FROM`` that names an earlier stage is a build step, not a dependency, and is skipped.
    ``ARG`` defaults declared before the first ``FROM`` are substituted.
    """
    out: dict[str, Dep] = {}
    args: dict[str, str] = {}
    stages: list[str] = []
    anonymous = 0
    for raw in logical_lines(text):
        line = raw.strip()
        arg = re.match(r'(?i)ARG\s+(\w+)=(\S*)', line)
        if arg and not stages:
            args[arg[1]] = arg[2].strip('"\'')
        match = re.match(r'(?i)FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$', line)
        if not match:
            if re.match(r'(?i)FROM\b', line):
                raise ManifestError(f'{path}: unparsable FROM line: {line!r}')
            continue
        ref, alias = _expand_args(match[1], args), match[2]
        pattern = re.sub(r'\\\$(\\\{)?\w+(\\\})?', '.+', re.escape(ref))
        internal = ref.lower() == 'scratch' or any(
            re.fullmatch(pattern, s, re.IGNORECASE) for s in stages
        )
        if alias:
            stages.append(alias)
        if internal:
            continue
        if alias:
            key = f'FROM {alias}'
        else:
            anonymous += 1
            key = f'FROM #{anonymous}'
        out[key] = Dep(key, ref, image_tag_versions(ref), 'image', direct=None)
    return out


def parse_compose(path: str, text: str) -> dict[str, Dep]:
    """``image:`` values of a compose file keyed by their YAML path (``services/backend``)."""
    out: dict[str, Dep] = {}
    stack: list[tuple[int, str]] = []
    for raw in text.replace('\r\n', '\n').splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        if '\t' in raw[: len(raw) - len(raw.lstrip())]:
            raise ManifestError(f'{path}: tab indentation is not valid YAML')
        match = re.match(r'^(\s*)(?:-\s+)?([^\s:#][^:]*?)\s*:(?:\s+(.*))?$', raw)
        if not match:
            continue
        indent, key, value = len(match[1]), match[2], strip_comment(match[3] or '')
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if key == 'image' and value:
            ref = value.strip('"\'')
            where = '/'.join(k for _, k in stack) or '(root)'
            out[where] = Dep(where, ref, image_tag_versions(ref), 'image', direct=None)
        elif not value or value[0] in '&|>':
            stack.append((indent, key))
    return out
