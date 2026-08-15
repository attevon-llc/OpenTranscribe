#!/usr/bin/env python3
"""Report API routes that no test references — accurately enough to act on.

Why this is a script and not a grep
-----------------------------------
The obvious version — search the test tree for each route's literal path — reported 141
uncovered routes on a tree where two thirds of them were in fact covered. The new suites
build their URLs from a module constant::

    _BASE = "/api/user-settings"
    client.put(f"{_BASE}/download", ...)

so the literal ``/api/user-settings/download`` never appears anywhere, and a literal scan
calls a thoroughly tested route untested. A metric that is wrong in the reassuring
direction is bad; one that is wrong in the alarming direction wastes real work and
discredits the number, which is what happened here.

So this resolves string constants, parametrize tables and f-string / ``str.format``
templates per test file and reconstructs the URLs actually handed to an HTTP client call,
before matching. It still measures *reference*, not execution — an upper bound on
"untested" — and says so in its output rather than letting the reader forget.

Why the evidence is CALL-scoped, not file-scoped
------------------------------------------------
The previous version pooled every ``ast.Constant`` in a test file. That made the metric
self-defeating three separate ways, all of which scored an untested route as covered, and
all of which are ``--selftest`` cases now:

1. **The HTTP method was not part of the match key.** Routes were enumerated as
   ``(method, path)`` but matched on ``path`` alone, so a POST test "covered" the DELETE
   route at the same path. Five SCIM routes — including the RFC 7644 *replace* verb
   ``PUT /scim/v2/Users/{user_id}`` — scored covered while no test issued those verbs.
2. **An unresolved f-string wildcard could stand in for a LITERAL route segment.**
   ``f"/api/files/{uuid4()}/{suffix}"`` became ``/api/files/<any>/<any>`` and matched every
   four-segment route under ``/api/files/``. A wildcard now matches only a route segment
   that is itself a parameter (``{...}``), never a literal.
3. **Any string constant counted, including route-inventory tables.** The xfail/allowlist
   dicts in ``tests/unit/test_route_has_a_caller.py`` and friends *enumerate route
   templates*; pooling them meant adding a route to an xfail table marked it permanently
   "covered". Requiring the string to reach an HTTP client call fixes this at the root
   rather than by name-listing the three modules that happen to hold such tables today —
   a blocklist would go stale the first time a fourth inventory module is added, and this
   version also excludes the same shape appearing in a docstring or a skip reason.

Usage::

    python3 scripts/audit-route-coverage.py                 # summary by router prefix
    python3 scripts/audit-route-coverage.py --list           # every uncovered route
    python3 scripts/audit-route-coverage.py --json           # machine-readable
    python3 scripts/audit-route-coverage.py --prefix admin   # one cluster
    python3 scripts/audit-route-coverage.py --selftest       # check the matcher itself
    python3 scripts/audit-route-coverage.py --fail-on-uncovered   # exit 1 if any uncovered

``--fail-on-uncovered`` is OFF by default: the plain run is a report and keeps exiting 0 so
existing callers do not start failing. Turn it on to use this as a gate once the backlog it
reports is at zero.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import pathlib
import re
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
_BACKEND = _REPO / 'backend'

#: Sentinel for an f-string / format field whose value could not be resolved. It stands in
#: for exactly one *parameter* segment — never for a literal one (defect 2 above).
_WILD = '\x00'

#: ``client.get(...)`` and friends. ``request`` and ``websocket_connect`` are handled
#: separately because their method comes from an argument, not from the attribute name.
_VERB_ATTRS = frozenset({'get', 'post', 'put', 'patch', 'delete', 'head', 'options'})
_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'})
_WEBSOCKET = 'WEBSOCKET'

#: ``"{name}"`` / ``"{0}"`` / ``"{}"`` in a ``str.format`` template.
_FORMAT_FIELD = re.compile(r'\{[^{}]*\}')

#: Rounds of binding resolution. Two is enough for ``_BASE = "/api/x"`` followed by
#: ``_SUB = f"{_BASE}/y"``; the extra rounds cover longer chains and cost one AST walk each.
_ENV_ROUNDS = 4

#: Cap on the alternatives an f-string may expand to before the multi-valued pieces
#: collapse to a wildcard. A parametrize table crossed with a base constant is otherwise a
#: combinatorial explosion for no gain in truth.
_EXPAND_CAP = 32


def _load_app():
    """Import the FastAPI app with the env it needs, without touching a real deployment."""
    scratch = pathlib.Path(os.environ.get('TMPDIR', '/tmp')) / 'ot-route-audit'
    for sub in ('uploads', 'models', 'temp'):
        (scratch / sub).mkdir(parents=True, exist_ok=True)
    os.environ.update(
        TESTING='true',
        SKIP_CELERY='true',
        UPLOAD_DIR=str(scratch / 'uploads'),
        MODEL_CACHE_DIR=str(scratch / 'models'),
        TEMP_DIR=str(scratch / 'temp'),
    )
    sys.path.insert(0, str(_BACKEND))
    from app.main import app  # noqa: PLC0415  (import must follow the env setup)

    return app


# --------------------------------------------------------------------------------------
# Resolving names to the strings they can hold
# --------------------------------------------------------------------------------------


class _Env:
    """The strings each name in one module can hold, plus the tables it can iterate.

    Deliberately flow-insensitive and scope-insensitive: a test file's ``_BASE`` means the
    same thing everywhere, and pretending to interpret the module buys nothing. Over-
    resolution here is safe in the reporting direction — it can only add candidate URLs,
    and every candidate still has to match a route structurally *and* by method.
    """

    def __init__(self) -> None:
        self.names: dict[str, set[str]] = {}
        self.tables: dict[str, ast.expr] = {}
        #: ``fn = getattr(client, method)`` — the verbs ``fn`` may dispatch to.
        self.aliases: dict[str, set[str | None]] = {}
        #: ``def _segment_url(a, b): return f"{FILES}/{a}/transcript/segments/{b}"`` — the
        #: URLs a helper (or fixture) can return, with its parameters as wildcards.
        self.helpers: dict[str, set[str]] = {}

    def bind(self, name: str, values: set[str]) -> None:
        if values:
            self.names.setdefault(name, set()).update(values)

    def scoped(self, params: list[str]) -> _Env:
        """A view of this env with *params* shadowed by a wildcard (a callee's arguments)."""
        inner = _Env()
        inner.names = dict(self.names) | {p: {_WILD} for p in params}
        inner.tables = self.tables
        inner.aliases = self.aliases
        inner.helpers = self.helpers
        return inner


def _strings(node: ast.expr | None, env: _Env) -> set[str]:
    """Every string *node* could evaluate to, with ``_WILD`` for unresolvable pieces."""
    if node is None:
        return set()
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.Name):
        return set(env.names.get(node.id, ()))
    if isinstance(node, ast.Await):
        return _strings(node.value, env)
    if isinstance(node, ast.JoinedStr):
        return _expand_joined(node, env)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return set(env.helpers.get(node.func.id, ()))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _strings(node.left, env), _strings(node.right, env)
        if not left or not right or len(left) * len(right) > _EXPAND_CAP:
            return set()
        return {a + b for a in left for b in right}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == 'format':
            # `"/api/x/{id}".format(id=...)` -- the field is exactly one segment's worth
            # of unknown, same as an f-string expression.
            return {_FORMAT_FIELD.sub(_WILD, base) for base in _strings(node.func.value, env)}
        if node.func.attr in {'lstrip', 'rstrip', 'strip'}:
            return _strings(node.func.value, env)
    return set()


def _expand_joined(node: ast.JoinedStr, env: _Env) -> set[str]:
    """Rebuild an f-string, substituting resolvable pieces and wildcarding the rest."""
    pieces: list[list[str]] = []
    for piece in node.values:
        if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
            pieces.append([piece.value])
        elif isinstance(piece, ast.FormattedValue):
            resolved = sorted(_strings(piece.value, env))
            pieces.append(resolved if resolved else [_WILD])
        else:  # pragma: no cover - JoinedStr holds only these two node types
            pieces.append([_WILD])

    total = 1
    for alternatives in pieces:
        total *= len(alternatives)
    if total > _EXPAND_CAP:
        pieces = [alts if len(alts) == 1 else [_WILD] for alts in pieces]

    out = ['']
    for alternatives in pieces:
        out = [prefix + alt for prefix in out for alt in alternatives]
    return set(out)


def _iter_strings(node: ast.expr, env: _Env) -> set[str]:
    """The strings iterating *node* would yield (a list/tuple/set literal, or a name for one)."""
    if isinstance(node, ast.Name):
        node = env.tables.get(node.id, node)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        out: set[str] = set()
        for element in node.elts:
            out |= _strings(_unwrap_param(element), env)
        return out
    return set()


def _unwrap_param(node: ast.expr) -> ast.expr:
    """``pytest.param("a", "b", id=...)`` -> the tuple ``("a", "b")``."""
    if isinstance(node, ast.Call):
        func = node.func
        named = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
        if named == 'param':
            return ast.Tuple(elts=list(node.args), ctx=ast.Load())
    return node


def _collect_parametrize(call: ast.Call, env: _Env) -> None:
    """Bind the argnames of a ``@pytest.mark.parametrize`` to their literal cells.

    Table-driven suites are how this repo covers whole route families
    (``("post", "cancel"), ("get", "status-detail")``); without this the URLs they build
    resolve to a bare wildcard and every route in the family reads as unreferenced.
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == 'parametrize') or len(call.args) < 2:
        return
    names_node, table = call.args[0], call.args[1]
    if isinstance(names_node, ast.Constant) and isinstance(names_node.value, str):
        argnames = [n.strip() for n in names_node.value.split(',') if n.strip()]
    elif isinstance(names_node, ast.List | ast.Tuple):
        argnames = [
            e.value
            for e in names_node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    else:
        return

    if isinstance(table, ast.Name):
        table = env.tables.get(table.id, table)
    if not isinstance(table, ast.List | ast.Tuple):
        return

    for row in table.elts:
        row = _unwrap_param(row)
        if len(argnames) == 1:
            env.bind(argnames[0], _strings(row, env))
            continue
        if not isinstance(row, ast.List | ast.Tuple):
            continue
        for name, cell in zip(argnames, row.elts, strict=False):
            env.bind(name, _strings(cell, env))


def _collect_helpers(node: ast.FunctionDef | ast.AsyncFunctionDef, env: _Env) -> None:
    """Bind a URL-builder helper / fixture to the URLs it can return.

    ``_segment_url(file_uuid, segment_uuid)`` returning
    ``f"{FILES}/{file_uuid}/transcript/segments/{segment_uuid}"`` is how this suite names
    its longest routes; without this the caller's URL resolves to nothing and a genuinely
    tested route reads as unreferenced (two were, before this was added). Parameters are
    wildcards, so a helper can only ever satisfy a route *parameter* — never a literal.
    """
    args = node.args
    params = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    inner = env.scoped(params)
    returned: set[str] = set()
    for statement in ast.walk(node):
        if isinstance(statement, ast.Return) and statement.value is not None:
            returned |= _strings(statement.value, inner)
    if not returned:
        return
    env.helpers.setdefault(node.name, set()).update(returned)
    # A fixture is injected by NAME, so it also has to resolve as a bare name.
    decorators = ' '.join(ast.dump(d) for d in node.decorator_list)
    if "'fixture'" in decorators:
        env.bind(node.name, returned)


def _collect_bindings(tree: ast.AST, env: _Env) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _collect_helpers(node, env)
        elif isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            if not targets:
                continue
            if isinstance(node.value, ast.List | ast.Tuple | ast.Set):
                for target in targets:
                    env.tables[target.id] = node.value
            verbs = _getattr_verbs(node.value, env)
            values = _strings(node.value, env)
            for target in targets:
                env.bind(target.id, values)
                if verbs is not None:
                    env.aliases.setdefault(target.id, set()).update(verbs)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            env.bind(node.target.id, _strings(node.value, env))
        elif (
            isinstance(node, ast.For | ast.AsyncFor)
            and isinstance(node.target, ast.Name)
            or isinstance(node, ast.comprehension)
            and isinstance(node.target, ast.Name)
        ):
            env.bind(node.target.id, _iter_strings(node.iter, env))
        elif isinstance(node, ast.Call):
            _collect_parametrize(node, env)


def _build_env(tree: ast.AST) -> _Env:
    env = _Env()
    for _ in range(_ENV_ROUNDS):
        before = (
            {name: set(values) for name, values in env.names.items()},
            {name: set(values) for name, values in env.helpers.items()},
        )
        _collect_bindings(tree, env)
        if (env.names, env.helpers) == before:
            break
    return env


# --------------------------------------------------------------------------------------
# Turning HTTP client calls into (method, path) evidence
# --------------------------------------------------------------------------------------

#: ``(method or None, path segments)``. ``None`` means the call's verb could not be
#: resolved, and matches any method — the one remaining permissive case, counted in
#: ``--json`` so it cannot grow unnoticed.
_Evidence = tuple[str | None, tuple[str, ...]]


def _getattr_verbs(node: ast.expr, env: _Env) -> set[str | None] | None:
    """``getattr(client, method)`` -> the verbs it can dispatch to, else ``None``."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return None
    if node.func.id != 'getattr' or len(node.args) < 2:
        return None
    resolved = {v.upper() for v in _strings(node.args[1], env) if v.upper() in _METHODS}
    return resolved or {None}  # type: ignore[return-value]


def _argument(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _call_target(call: ast.Call, env: _Env) -> tuple[set[str | None], ast.expr | None] | None:
    """The verbs and URL expression of an HTTP client call, or ``None`` if it is not one."""
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in _VERB_ATTRS:
            return {func.attr.upper()}, _argument(call, 0, 'url')
        if func.attr == 'websocket_connect':
            return {_WEBSOCKET}, _argument(call, 0, 'url')
        if func.attr == 'request':
            method_node = _argument(call, 0, 'method')
            verbs = {v.upper() for v in _strings(method_node, env) if v.upper() in _METHODS}
            url = call.args[1] if len(call.args) > 1 else _argument(call, 99, 'url')
            return (verbs or {None}), url  # type: ignore[return-value]
        return None
    verbs = _getattr_verbs(func, env)
    if verbs is not None:  # getattr(client, method)("/api/x")
        return verbs, _argument(call, 0, 'url')
    if isinstance(func, ast.Name) and func.id in env.aliases:
        return set(env.aliases[func.id]), _argument(call, 0, 'url')
    return None


def _candidate_paths(text: str) -> list[tuple[str, ...]]:
    """Every path-shaped substring in *text*, split into segments."""
    out: list[tuple[str, ...]] = []
    for raw in re.findall(r'/[A-Za-z0-9_\-./{}\x00]*', text):
        path = raw.split('?')[0].rstrip('/') or '/'
        out.append(tuple(path.split('/')))
    return out


def _evidence_from_source(source: str) -> set[_Evidence]:
    """Every ``(method, path)`` an HTTP client call in *source* could issue."""
    tree = ast.parse(source)
    env = _build_env(tree)
    evidence: set[_Evidence] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _call_target(node, env)
        if target is None:
            continue
        verbs, url_node = target
        for text in _strings(url_node, env):
            for segments in _candidate_paths(text):
                for verb in verbs:
                    evidence.add((verb, segments))
    return evidence


def _segments_match(route: tuple[str, ...], candidate: tuple[str, ...]) -> bool:
    """Structural comparison, segment by segment.

    Deliberately NOT a substring match. A permissive
    ``^.*/api/tasks/[^/]*.*$`` counts ``/api/tasks/{task_id}`` as covered by any test that
    merely names ``/api/tasks/system/fix-file/x`` — which under-reports the gap and is how
    the previous version of this metric produced a number that was too flattering to act on.
    A route path and a URL match only if they have the same shape.

    A wildcard segment (an f-string expression this script could not resolve) may stand in
    only for a route *parameter*. Letting it satisfy a literal made
    ``f"/api/files/{uuid4()}/{suffix}"`` cover every four-segment route under
    ``/api/files/`` — including the ones a test was asserting are **404/removed**, so a
    test proving a route is gone counted as coverage for routes that exist.
    """
    if len(route) != len(candidate):
        return False
    for r_seg, c_seg in zip(route, candidate, strict=True):
        r_is_param = r_seg.startswith('{') and r_seg.endswith('}')
        if _WILD in c_seg:
            if not r_is_param:
                return False
            continue
        if r_is_param:
            continue  # route parameter: any single concrete segment
        if r_seg != c_seg:
            return False
    return True


def _method_matches(route_method: str, evidence_method: str | None) -> bool:
    """A POST test does not exercise the DELETE route at the same path."""
    return evidence_method is None or evidence_method == route_method


def _covered(method: str, path: str, evidence: dict[int, list[_Evidence]]) -> bool:
    segments = tuple((path.rstrip('/') or '/').split('/'))
    return any(
        _method_matches(method, ev_method) and _segments_match(segments, ev_segments)
        for ev_method, ev_segments in evidence.get(len(segments), ())
    )


# --------------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------------

#: Must-fire / must-stay-clean cases for the matcher. A metric with no self-test is
#: indistinguishable from a broken one: the first version of this script under-reported by
#: two thirds and read as good news; the second over-matched ``/api/tasks/{task_id}``
#: against ``/api/tasks/system/fix-file/x``; the third reported **zero** uncovered routes
#: out of 490 while ignoring the HTTP method, letting a wildcard satisfy a literal segment,
#: and accepting a route-inventory table as evidence. Every one of those is a case below,
#: each paired with a must-stay-clean twin so the fix cannot be "achieved" by matching
#: nothing.
_SELFTEST: list[tuple[str, str, str, str, bool]] = [
    # (description, route method, route path, test-source text, should_be_covered)
    (
        'plain literal',
        'GET',
        '/api/groups',
        'client.get("/api/groups")',
        True,
    ),
    (
        'base constant resolved out of an f-string -- the whole reason this is not a grep',
        'PUT',
        '/api/user-settings/download',
        '_BASE = "/api/user-settings"\nclient.put(f"{_BASE}/download", json={})',
        True,
    ),
    (
        'route parameter matches any single segment',
        'GET',
        '/api/files/{file_uuid}/info',
        'client.get(f"/api/files/{file.uuid}/info")',
        True,
    ),
    (
        'a DEEPER path must NOT cover a shallower route (the over-match regression)',
        'POST',
        '/api/tasks/{task_id}',
        'client.post("/api/tasks/system/fix-file/abc")',
        False,
    ),
    (
        'a SHALLOWER path must not cover a deeper route either',
        'GET',
        '/api/search/models/neural/status',
        'client.get("/api/search/models")',
        False,
    ),
    (
        'a sibling segment is not a match',
        'GET',
        '/api/search/suggestions',
        'client.get("/api/search/count")',
        False,
    ),
    (
        'trailing slash parity -- nginx normalises these, so they are one route',
        'GET',
        '/api/tags/',
        'client.get("/api/tags")',
        True,
    ),
    (
        'unresolved f-string expression stands in for exactly one segment',
        'DELETE',
        '/api/admin/users/{user_uuid}',
        'client.delete(f"/api/admin/users/{make_uuid()}")',
        True,
    ),
    (
        '...but not for two',
        'DELETE',
        '/api/admin/users/{user_uuid}/role',
        'client.delete(f"/api/admin/users/{make_uuid()}")',
        False,
    ),
    # -- defect 1: the HTTP method is part of the match key -----------------------------
    (
        'MUST FIRE: a POST test does not cover the DELETE route at the same path',
        'DELETE',
        '/scim/v2/Groups/{group_id}',
        'client.post(f"/scim/v2/Groups/{gid}", json={})',
        False,
    ),
    (
        'must stay clean: ...and the POST route at that same path IS covered by it',
        'POST',
        '/scim/v2/Groups/{group_id}',
        'client.post(f"/scim/v2/Groups/{gid}", json={})',
        True,
    ),
    (
        'must stay clean: the verb the test actually issues still counts',
        'POST',
        '/scim/v2/Groups',
        'client.post("/scim/v2/Groups", json={})',
        True,
    ),
    (
        'MUST FIRE: RFC 7644 replace -- a POST to the collection is not a PUT to the member',
        'PUT',
        '/scim/v2/Users/{user_id}',
        'client.post("/scim/v2/Users", json={})\nclient.get(f"/scim/v2/Users/{uid}")',
        False,
    ),
    (
        'client.request("DELETE", url) resolves its verb from the first argument',
        'DELETE',
        '/api/files/{file_id}',
        'client.request("DELETE", f"/api/files/{fid}")',
        True,
    ),
    (
        '...and only that verb',
        'PUT',
        '/api/files/{file_id}',
        'client.request("DELETE", f"/api/files/{fid}")',
        False,
    ),
    # -- defect 2: a wildcard may not stand in for a LITERAL route segment ---------------
    (
        'MUST FIRE: an unresolved suffix does not cover every sibling route',
        'GET',
        '/api/files/{file_uuid}/thumbnail',
        'client.get(f"/api/files/{fid}/{suffix}")',
        False,
    ),
    (
        'must stay clean: a wildcard DOES cover a route parameter in that position',
        'GET',
        '/api/admin/users/{user_uuid}/{setting_key}',
        'client.get(f"/api/admin/users/{uid}/{key}")',
        True,
    ),
    (
        'MUST FIRE: a test ASSERTING a route is removed must not cover live siblings',
        'GET',
        '/api/files/{file_uuid}/stream-url',
        (
            'def test_legacy_byte_proxy_routes_are_gone(client, suffix):\n'
            '    r = client.get(f"/api/files/{mf.uuid}/{suffix}")\n'
            '    assert r.status_code == 404\n'
        ),
        False,
    ),
    (
        'must stay clean: naming the suffixes makes the ones really exercised count',
        'GET',
        '/api/files/{file_uuid}/content',
        (
            '@pytest.mark.parametrize("suffix", ["video", "content"])\n'
            'def test_legacy_byte_proxy_routes_are_gone(client, suffix):\n'
            '    r = client.get(f"/api/files/{mf.uuid}/{suffix}")\n'
            '    assert r.status_code == 404\n'
        ),
        True,
    ),
    (
        'must stay clean: a parametrize table names the suffixes it really exercises',
        'POST',
        '/api/files/{file_id}/cancel',
        (
            '@pytest.mark.parametrize("method,suffix", [("post", "cancel"), ("get", "status")])\n'
            'def test_subresources(client, method, suffix):\n'
            '    getattr(client, method)(f"/api/files/{uuid4()}/{suffix}")\n'
        ),
        True,
    ),
    (
        '...and the verb from that same table still discriminates',
        'DELETE',
        '/api/files/{file_id}/cancel',
        (
            '@pytest.mark.parametrize("method,suffix", [("post", "cancel"), ("get", "status")])\n'
            'def test_subresources(client, method, suffix):\n'
            '    getattr(client, method)(f"/api/files/{uuid4()}/{suffix}")\n'
        ),
        False,
    ),
    # -- defect 3: evidence must reach an HTTP client call ------------------------------
    (
        'MUST FIRE: a route-inventory table is not a test of the route it lists',
        'GET',
        '/api/admin/tasks',
        '_XFAIL = {"/api/admin/tasks": "no caller yet"}\n'
        'def test_every_route_has_a_caller():\n'
        '    assert set(_XFAIL) <= known_routes\n',
        False,
    ),
    (
        'must stay clean: the same literal DOES count once a client call issues it',
        'GET',
        '/api/admin/tasks',
        '_XFAIL = {"/api/admin/tasks": "no caller yet"}\nclient.get("/api/admin/tasks")\n',
        True,
    ),
    (
        'MUST FIRE: a docstring or skip reason naming a route is not evidence',
        'POST',
        '/api/admin/reindex',
        '"""Covers POST /api/admin/reindex eventually."""\n'
        'pytest.skip("/api/admin/reindex is flaky")\n',
        False,
    ),
    (
        'must stay clean: a URL held in a module list and looped over IS evidence',
        'GET',
        '/api/admin/reindex',
        '_ENDPOINTS = ["/api/admin/reindex", "/api/admin/stats"]\n'
        'def test_all(client):\n'
        '    for url in _ENDPOINTS:\n'
        '        client.get(url)\n',
        True,
    ),
    (
        'must stay clean: a URL-builder helper resolves through its return statement',
        'PUT',
        '/api/files/{file_uuid}/transcript/segments/{segment_uuid}',
        'FILES = "/api/files"\n'
        'def _segment_url(file_uuid, segment_uuid):\n'
        '    return f"{FILES}/{file_uuid}/transcript/segments/{segment_uuid}"\n'
        'def test_edit(client):\n'
        '    client.put(_segment_url(a, b), json={})\n',
        True,
    ),
    (
        "...but the helper's PARAMETERS are wildcards, so it cannot satisfy a literal",
        'PUT',
        '/api/files/{file_uuid}/transcript/segments/published',
        'FILES = "/api/files"\n'
        'def _segment_url(file_uuid, segment_uuid):\n'
        '    return f"{FILES}/{file_uuid}/transcript/segments/{segment_uuid}"\n'
        'def test_edit(client):\n'
        '    client.put(_segment_url(a, b), json={})\n',
        False,
    ),
    # -- websocket routes are evaluated, not dropped ------------------------------------
    (
        'websocket_connect covers a websocket route',
        _WEBSOCKET,
        '/api/ws',
        'with client.websocket_connect("/api/ws") as ws:\n    ws.send_json({})\n',
        True,
    ),
    (
        '...and an HTTP GET to the same path does not',
        _WEBSOCKET,
        '/api/ws',
        'client.get("/api/ws")',
        False,
    ),
]


def _selftest() -> int:
    """Run the matcher against in-memory cases; exit non-zero on any wrong verdict."""
    failures = 0
    for description, route_method, route_path, source, expected in _SELFTEST:
        evidence = collections.defaultdict(list)
        for item in _evidence_from_source(source):
            evidence[len(item[1])].append(item)
        covered = _covered(route_method, route_path, evidence)
        ok = covered == expected
        failures += not ok
        mark = 'PASS' if ok else 'FAIL'
        want = 'covered' if expected else 'NOT covered'
        print(
            f'  [{mark}] {description}  (expected {want}, got '
            f'{"covered" if covered else "NOT covered"})'
        )
    print()
    if failures:
        print(f'{failures} self-test case(s) FAILED — the metric cannot be trusted')
        return 1
    print(f'all {len(_SELFTEST)} self-test cases pass')
    return 0


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def _collect_routes(app) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(http_routes, websocket_routes)`` as ``(method, path)`` pairs."""
    http: list[tuple[str, str]] = []
    websockets: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, 'path', None)
        if not path:
            continue
        methods = getattr(route, 'methods', None) or set()
        if not methods:
            # Starlette's WebSocketRoute has no `.methods`. The previous version dropped
            # these silently, so they were neither counted nor reported.
            if hasattr(route, 'app') or hasattr(route, 'endpoint'):
                websockets.append((_WEBSOCKET, path))
            continue
        for method in sorted(methods - {'HEAD', 'OPTIONS'}):
            http.append((method, path))
    return http, websockets


def _scan_tests() -> tuple[dict[int, list[_Evidence]], int]:
    evidence: dict[int, list[_Evidence]] = collections.defaultdict(list)
    test_files = 0
    for file in sorted((_BACKEND / 'tests').rglob('*.py')):
        try:
            items = _evidence_from_source(file.read_text(errors='ignore'))
        except SyntaxError:
            continue
        test_files += 1
        for item in items:
            evidence[len(item[1])].append(item)
    return evidence, test_files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--list', action='store_true', help='print every uncovered route')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--prefix', help='only routes whose path contains this string')
    ap.add_argument(
        '--selftest', action='store_true', help='check the matcher against in-memory cases and exit'
    )
    ap.add_argument(
        '--fail-on-uncovered',
        action='store_true',
        help='exit 1 when any route is uncovered (default: report only, exit 0)',
    )
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    app = _load_app()
    routes, ws_routes = _collect_routes(app)
    evidence, test_files = _scan_tests()

    uncovered = [(m, p) for m, p in routes if not _covered(m, p, evidence)]
    ws_uncovered = [(m, p) for m, p in ws_routes if not _covered(m, p, evidence)]

    if args.prefix:
        uncovered = [(m, p) for m, p in uncovered if args.prefix in p]
        ws_uncovered = [(m, p) for m, p in ws_uncovered if args.prefix in p]

    failed = bool(args.fail_on_uncovered and (uncovered or ws_uncovered))

    if args.json:
        print(
            json.dumps(
                {
                    'total_routes': len(routes),
                    'unreferenced': len(uncovered),
                    'routes': [{'method': m, 'path': p} for m, p in uncovered],
                    'websocket_routes': len(ws_routes),
                    'websocket_unreferenced': [{'method': m, 'path': p} for m, p in ws_uncovered],
                    'evidence_with_unresolved_method': sum(
                        1 for items in evidence.values() for method, _ in items if method is None
                    ),
                },
                indent=2,
            )
        )
        return 1 if failed else 0

    print(f'{len(routes)} routes, {test_files} test files scanned')
    print(f'{len(uncovered)} route(s) with no test reference')
    print(
        f'{len(ws_routes)} WebSocket route(s) (no HTTP method), '
        f'{len(ws_uncovered)} with no test reference'
        + (': ' + ', '.join(p for _, p in ws_uncovered) if ws_uncovered else '')
    )
    print("  NOTE: this measures REFERENCE, not execution — an upper bound on 'untested'.")
    print('  A route counted here is definitely not named by any test; a route NOT counted')
    print('  may still only be touched incidentally.\n')

    by_prefix: collections.Counter[str] = collections.Counter(
        p.split('/')[2] if p.count('/') > 2 else p for _, p in uncovered
    )
    for prefix, count in by_prefix.most_common():
        print(f'  {count:3d}  {prefix}')

    if args.list:
        print()
        for method, path in sorted(uncovered, key=lambda x: (x[1], x[0])):
            print(f'  {method:9s} {path}')

    if failed:
        print('\nFAIL: --fail-on-uncovered and at least one route has no test reference')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
