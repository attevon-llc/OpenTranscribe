"""AST scan: find slow calls reachable while a DB session or transaction is open."""

from __future__ import annotations

import ast
from pathlib import Path

from session_lifetime_audit.findings import Finding
from session_lifetime_audit.rules import RULES, SLOW_CLIENTS, Rule

_SESSION_OPENERS = frozenset(
    {
        'session_scope',
        'SessionLocal',
        'get_db_session',
        'transaction',
        'session_factory',
        '_short_session',
    }
)

#: Parameter annotations that mean "this function was handed someone else's transaction".
_SESSION_ANNOTATIONS = frozenset({'Session', 'AsyncSession', 'scoped_session'})

#: Method names that are never slow, whatever object they are called on. These suppress
#: PATH-based matches only — an explicitly named method in a rule still fires. Without this,
#: ``opensearch_result.get("bluf")`` (a dict read on a variable whose NAME contains
#: "opensearch") and ``llm_service.close()`` both read as network calls, and five of them in
#: one endpoint inflate the count fivefold. A detector nobody believes gets switched off.
_BENIGN_LAST_NAMES = frozenset(
    {
        'get',
        'keys',
        'items',
        'values',
        'pop',
        'close',
        'append',
        'extend',
        'copy',
        'clear',
        'strip',
        'lower',
        'upper',
        'format',
        'join',
        'split',
    }
)


def dotted_name(node: ast.AST) -> str:
    """Best-effort dotted path for a call target (``a.b.c`` from ``a.b.c(...)``)."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    elif isinstance(current, ast.Call):
        parts.append(dotted_name(current.func))
    return '.'.join(reversed(parts))


def last_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ''


def client_bindings(tree: ast.AST) -> dict[str, str]:
    """Local names bound from a :data:`SLOW_CLIENTS` constructor, mapped to its category.

    Module-wide rather than per function: a handler module binds the same service under the
    same name in each handler (``search_service = HybridSearchService()``), and a name that
    meant a slow client in one function and something else in another would be a reading
    hazard of its own.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or last_name(value.func) not in SLOW_CLIENTS:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                bound[target.id] = SLOW_CLIENTS[last_name(value.func)]
    return bound


def _client_rule(call: ast.Call, bound: dict[str, str]) -> Rule | None:
    """The rule for a PUBLIC method called on a slow client instance, or None."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr.startswith('_'):
        return None
    if func.attr in _BENIGN_LAST_NAMES:
        return None
    receiver = func.value
    category: str | None = None
    if isinstance(receiver, ast.Call) and last_name(receiver.func) in SLOW_CLIENTS:
        category = SLOW_CLIENTS[last_name(receiver.func)]
    elif isinstance(receiver, ast.Name) and receiver.id in bound:
        category = bound[receiver.id]
    if category is None:
        return None
    return next((rule for rule in RULES if rule.category == category), None)


def classify(call: ast.Call, bound: dict[str, str] | None = None) -> Rule | None:
    """Return the rule this call trips, or None.

    ``bound`` is :func:`client_bindings` for the module the call is in.
    """
    name = last_name(call.func)
    dotted = dotted_name(call.func).lower()
    for rule in RULES:
        if name in rule.names:
            return rule
    client_rule = _client_rule(call, bound or {})
    if client_rule is not None:
        return client_rule
    if name in _BENIGN_LAST_NAMES:
        return None
    for rule in RULES:
        if any(fragment in dotted for fragment in rule.paths):
            return rule
    return None


def opens_session(item: ast.withitem) -> bool:
    """True when a ``with`` item opens a DB session."""
    ctx = item.context_expr
    if isinstance(ctx, ast.Call):
        return last_name(ctx.func) in _SESSION_OPENERS
    return last_name(ctx) in _SESSION_OPENERS


#: Session methods that put the caller back INTO a transaction after a close.
_REARMS_TRANSACTION = frozenset(
    {'query', 'execute', 'add', 'delete', 'merge', 'refresh', 'flush', 'scalar', 'get'}
)


def release_line(fn: ast.FunctionDef | ast.AsyncFunctionDef, param: str) -> int | None:
    """Line after which *param*'s transaction is released, or None if it never is.

    Walks the function's TOP-LEVEL statements in order with one bit of state. A
    ``<param>.close()`` releases; any later ``<param>.<db-method>()`` re-arms, because a
    handler that closes and then queries again is holding a transaction across whatever
    follows — the read → close → slow → REOPEN → write shape is correct, and the reopened
    part must still be checked.

    Deliberately top-level only. A close inside an ``if`` or a loop is conditional, and
    treating a conditional release as a release would let the real defect through on the
    branch that skips it — the direction this must not be wrong in.
    """
    released: int | None = None
    for stmt in fn.body:
        # Skip COMPOUND statements entirely, but walk EXPRESSIONS inside a simple one.
        #
        # Both halves are load-bearing. Not descending into `if`/`for`/`while`/`try`/`with`
        # is what keeps a CONDITIONAL `db.close()` from reading as an unconditional release
        # — on the branch that skips it the transaction is still held, which is the real
        # defect, and that is the direction this must not be wrong in.
        #
        # But within a simple statement the chain must be walked, because the session call
        # is rarely the outermost one: in `again = db.query(M).first()` the outer call is
        # `.first()`, whose base is `db.query(M)` rather than the name `db` — so a check
        # that only inspects the outermost call never sees the re-arm.
        if isinstance(stmt, ast.If | ast.For | ast.While | ast.Try | ast.With):
            continue
        if isinstance(stmt, ast.AsyncFor | ast.AsyncWith):
            continue

        for call in (n for n in ast.walk(stmt) if isinstance(n, ast.Call)):
            if not isinstance(call.func, ast.Attribute):
                continue
            value = call.func.value
            if not isinstance(value, ast.Name) or value.id != param:
                continue
            if call.func.attr == 'close':
                released = call.lineno
            elif released is not None and call.func.attr in _REARMS_TRANSACTION:
                released = None
    return released


def takes_session(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The name of a parameter that is somebody else's ``Session``, or None.

    Annotation first — that is unambiguous. The name fallback (``db``/``session``) exists
    because a third of this codebase's helpers are annotated only as ``db`` with no type;
    ``_perform_scan(db, source, ...)``, the function that hid the ``scan_single`` leak, is
    exactly that shape and an annotation-only rule would have missed it again.
    """
    args = fn.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        annotation = arg.annotation
        if annotation is not None and last_name(annotation) in _SESSION_ANNOTATIONS:
            return arg.arg
        if annotation is not None:
            # A parameter annotated as something else is NOT a session, whatever it is
            # called (`db: str` in a config helper).
            continue
        if arg.arg in ('db', 'db_session'):
            return arg.arg
    return None


def enclosing_scopes(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the qualified name of the function/class enclosing it."""
    owner: dict[ast.AST, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f'{prefix}.{child.name}' if prefix else child.name
                owner[child] = name
                walk(child, name)
            else:
                owner[child] = prefix
                walk(child, prefix)

    walk(tree, '')
    return owner


def nearest_scope(node: ast.AST, owner: dict[ast.AST, str], parents: dict[ast.AST, ast.AST]) -> str:
    current: ast.AST | None = node
    while current is not None:
        if current in owner and owner[current]:
            return owner[current]
        current = parents.get(current)
    return '<module>'


def scan_source(source: str, rel: str) -> list[Finding]:
    """Scan one module's source. Returns findings in deterministic (line) order."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            Finding('session-subprocess', rel, exc.lineno or 0, '<parse>', f'syntax error: {exc}')
        ]

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    owner = enclosing_scopes(tree)
    bound = client_bindings(tree)

    findings: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()

    def record(rule: Rule, call: ast.Call, scope: str, why: str) -> None:
        key = (rule.category, call.lineno, scope)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            Finding(
                category=rule.category,
                path=rel,
                line=call.lineno,
                scope=scope,
                detail=f'{dotted_name(call.func) or last_name(call.func)}() {why}',
            )
        )

    # --- body rules: a slow call lexically inside a `with session_scope()` -------------
    for node in ast.walk(tree):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        if not any(opens_session(item) for item in node.items):
            continue
        scope = nearest_scope(node, owner, parents)
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if not isinstance(inner, ast.Call):
                    continue
                rule = classify(inner, bound)
                if rule is not None:
                    record(rule, inner, scope, 'runs with a DB transaction open')
        # A `with ThreadPoolExecutor(...)` inside the session body is caught above by its
        # constructor call; `as_completed`/`submit` inside it are the same block.

    # --- interprocedural rule: a function that ACCEPTS a session and does slow work ----
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        param = takes_session(node)
        if param is None:
            continue
        scope = owner.get(node, node.name)
        # A released session is not a held one. THE CURE FOR AN ENDPOINT IS `db.close()`:
        # its session comes from `Depends(get_db)` and lives for the request, so it cannot
        # be restructured into a `with` block — the fix is read → close → slow work.
        # Without this, the rule flags a CORRECTLY FIXED endpoint, and the only way past it
        # is to rename the helper so its dotted path stops matching. That happened: a fix
        # was renamed `_live_snapshot_status` purely to dodge a path substring, while two
        # structurally identical fixes went clean because their names differed. A gate that
        # punishes the cure gets switched off, which is the failure this whole file exists
        # to prevent.
        released_after = release_line(node, param)
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            rule = classify(inner, bound)
            if rule is None:
                continue
            if released_after is not None and inner.lineno > released_after:
                continue
            key = ('session-param-slow-work', inner.lineno, scope)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                Finding(
                    category='session-param-slow-work',
                    path=rel,
                    line=inner.lineno,
                    scope=scope,
                    detail=(
                        f'{dotted_name(inner.func) or last_name(inner.func)}() runs inside a '
                        f'function that accepts a caller-owned session ({param!r})'
                    ),
                )
            )

    findings.sort(key=lambda f: (f.line, f.category))
    return findings


def scan_file(path: Path, root: Path) -> list[Finding]:
    try:
        source = path.read_text()
    except UnicodeDecodeError:
        return []
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    return scan_source(source, rel)
