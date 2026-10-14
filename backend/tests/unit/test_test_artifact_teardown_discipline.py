"""A test that creates a persistent store must remove it on EVERY path (issue #1195).

``unit/test_e2e_data_hygiene.py`` holds the e2e suite to "cleanup in a finally"; this holds the
rest of ``backend/tests`` to the same rule for the artifacts that live OUTSIDE the database
savepoint and outlive the process: OpenSearch indexes and ingest pipelines, MinIO buckets,
Postgres databases, docker containers/networks/volumes, and temp files/directories.

The rule, per creation call, within the function that makes it:

* the creation sits inside a ``try`` whose ``finally`` removes that kind of artifact, or
* the creation is the statement IMMEDIATELY before such a ``try`` (nothing that can fail in
  between: an index created and then bulk-seeded before the ``try`` leaks on a failed seed,
  and that shape was in eight suites), or
* the removal is registered up front (``addfinalizer`` / ``ExitStack.callback`` /
  ``atexit.register``) in the same function.

A helper that creates and RETURNS the artifact passes the obligation to its callers: calls to
it are treated as creations of the same kind, so the rule lands on whoever owns the lifetime.
A removal reached through a local helper (``_drop_real_bucket``) counts. What a SIGKILL leaves
behind is the sweep's job (``scripts/cleanup-test-data.py``); this guard is about failures.

``_ALLOWED`` needs a written reason per entry and a stale entry fails. Guard-the-guard tests
at the bottom prove each detector fires and that the accepted shapes stay clean, so the scan
cannot pass by matching nothing.
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _TESTS_ROOT.parents[1]

KINDS = ("index", "pipeline", "bucket", "database", "docker", "tmp")

#: ``<path relative to repo>::<function>::<kind>`` -> reason. Empty is the goal.
_ALLOWED: dict[str, str] = {}


# ---------------------------------------------------------------------------------------------
# Call classification
# ---------------------------------------------------------------------------------------------


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _owner_attr(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
        return func.value.attr
    return ""


def _strings(node: ast.AST) -> list[str]:
    """String literal elements of a list/tuple argv, or the parts of a (f-)string."""
    if isinstance(node, (ast.List, ast.Tuple)):
        out: list[str] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
            elif isinstance(elt, ast.Starred):
                out.extend(_strings(elt.value))
            else:
                out.append("<dynamic>")
        return out
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
    return []


def _argv(call: ast.Call) -> list[str]:
    for arg in call.args[:1]:
        words = _strings(arg)
        if isinstance(arg, (ast.List, ast.Tuple)):
            return words
    return []


def _sql(call: ast.Call) -> str:
    texts: list[str] = []
    for arg in [*call.args, *(k.value for k in call.keywords)]:
        for node in ast.walk(arg):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                texts.append(node.value)
    return " ".join(texts).upper()


def _docker_verb(argv: list[str]) -> tuple[str, ...]:
    if "docker" not in argv:
        return ()
    rest = argv[argv.index("docker") + 1 :]
    return tuple(w for w in rest[:3] if not w.startswith("-"))


def created_kind(call: ast.Call) -> str | None:
    name = _call_name(call)
    if name == "create" and _owner_attr(call) == "indices":
        return "index"
    if name == "_ensure_versioned_speaker_index":
        return "index"
    if name == "put_pipeline":
        return "pipeline"
    if name == "make_bucket":
        return "bucket"
    if name in {"mkdtemp", "mkstemp"}:
        return "tmp"
    if name == "NamedTemporaryFile":
        for kw in call.keywords:
            if kw.arg == "delete" and isinstance(kw.value, ast.Constant) and not kw.value.value:
                return "tmp"
        return None
    if "CREATE DATABASE" in _sql(call):
        return "database"
    verb = _docker_verb(_argv(call))
    if verb[:1] == ("run",) or verb[:2] in {("network", "create"), ("volume", "create")}:
        if verb[:1] == ("run",) and "--rm" in _argv(call) and "-d" not in _argv(call):
            return None  # foreground --rm: gone when the call returns
        return "docker"
    return None


def removed_kind(call: ast.Call) -> str | None:
    name = _call_name(call)
    if name == "delete" and _owner_attr(call) == "indices":
        return "index"
    if name == "delete_pipeline":
        return "pipeline"
    if name == "remove_bucket":
        return "bucket"
    if name in {"rmtree", "unlink", "remove"}:
        return "tmp"
    if "DROP DATABASE" in _sql(call):
        return "database"
    verb = _docker_verb(_argv(call))
    if verb[:1] in {("rm",), ("stop",), ("kill",)} or verb[:2] in {
        ("network", "rm"),
        ("volume", "rm"),
        ("compose", "down"),
    }:
        return "docker"
    return None


# ---------------------------------------------------------------------------------------------
# Per-module analysis
# ---------------------------------------------------------------------------------------------

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _own_calls(node: ast.AST) -> list[ast.Call]:
    """Calls lexically inside ``node`` but not inside a nested function definition."""
    out: list[ast.Call] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, _FUNC):
            continue
        if isinstance(child, ast.Call):
            out.append(child)
        stack.extend(ast.iter_child_nodes(child))
    return out


def _is_test_or_fixture(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if fn.name.startswith("test"):
        return True
    for dec in fn.decorator_list:
        for node in ast.walk(dec):
            if isinstance(node, (ast.Attribute, ast.Name)) and getattr(
                node, "attr", getattr(node, "id", "")
            ) in {"fixture", "yield_fixture"}:
                return True
    return False


@dataclass(frozen=True)
class Finding:
    path: str
    function: str
    kind: str
    lineno: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.function}::{self.kind}"


def _imports(tree: ast.Module) -> dict[str, str]:
    """Local name -> module stem (``throwaway_pg``) or ``stem.func`` it refers to."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            stem = node.module.rsplit(".", 1)[-1]
            for alias in node.names:
                # `from tests.integration import throwaway_pg` imports a module, and
                # `from tests.fixtures.mock_llm import helper` a function: record both readings.
                out[alias.asname or alias.name] = f"{stem}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name.rsplit(".", 1)[-1]
    return out


class _Analyzer:
    """Helper CREATORS are resolved per module (and through imports), never by bare name:
    a ``_put`` that makes a bucket in one module says nothing about every other ``_put``.
    Helper REMOVERS are matched by bare name, which can only make a scan more lenient."""

    def __init__(
        self,
        creators: dict[str, str],
        removers: dict[str, set[str]],
        module: str = "",
        imports: dict[str, str] | None = None,
    ) -> None:
        #: ``stem.func`` -> kind it creates (and hands to its caller)
        self.creators = creators
        #: helper function name -> kinds its body removes
        self.removers = removers
        self.module = module
        self.imports = imports or {}

    def _qualified(self, call: ast.Call) -> list[str]:
        func = call.func
        if isinstance(func, ast.Name):
            imported = self.imports.get(func.id)
            return [q for q in (imported, f"{self.module}.{func.id}") if q]
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = self.imports.get(func.value.id, func.value.id)
            return [f"{owner.rsplit('.', 1)[-1]}.{func.attr}"]
        return []

    def creates(self, call: ast.Call) -> str | None:
        direct = created_kind(call)
        if direct:
            return direct
        for qualified in self._qualified(call):
            if qualified in self.creators:
                if qualified.split(".", 1)[0] in self.patched:
                    return None  # the module's primitive is monkeypatched: nothing real runs
                return self.creators[qualified]
        return None

    patched: set[str] = set()

    def _patched_modules(self, fn: ast.AST) -> set[str]:
        """Modules whose attributes ``fn`` replaces (``monkeypatch.setattr(mod, "run", ...)``).
        A helper of that module called afterwards drives the stand-in, not docker/MinIO."""
        out: set[str] = set()
        for call in _own_calls(fn):
            if _call_name(call) == "setattr" and call.args:
                target = call.args[0]
                if isinstance(target, ast.Name):
                    out.add(self.imports.get(target.id, target.id).rsplit(".", 1)[-1])
        return out

    def removes(self, call: ast.Call) -> set[str]:
        kinds = set(self.removers.get(_call_name(call), set()))
        direct = removed_kind(call)
        if direct:
            kinds.add(direct)
        if _call_name(call) in {"addfinalizer", "callback", "register"}:
            for arg in call.args:
                for node in ast.walk(arg):
                    if isinstance(node, ast.Call):
                        kinds |= self.removes(node)
                    elif isinstance(node, (ast.Name, ast.Attribute)):
                        ref = node.id if isinstance(node, ast.Name) else node.attr
                        kinds |= self.removers.get(ref, set())
                        kinds |= {"tmp"} if ref in {"rmtree", "unlink", "remove"} else set()
        return kinds

    def finally_removes(self, tries: list[ast.Try]) -> set[str]:
        kinds: set[str] = set()
        for node in tries:
            for stmt in node.finalbody:
                for call in [*(_c for _c in [stmt] if isinstance(_c, ast.Call)), *_own_calls(stmt)]:
                    kinds |= self.removes(call)
        return kinds

    def unprotected(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, int]]:
        registered: set[str] = set()
        # A database created inside a throwaway SERVER (the session's isolated_pg container,
        # a module's own throwaway_pg) dies with that server, whose fixture removes it.
        params = {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs]}
        if params & _THROWAWAY_SERVER_PARAMS:
            registered.add("database")
        self.patched = self._patched_modules(fn)
        for call in _own_calls(fn):
            if _call_name(call) in {"addfinalizer", "callback", "register"}:
                registered |= self.removes(call)
        out: list[tuple[str, int]] = []
        self._walk_block(fn.body, [], registered, out)
        return out

    def _walk_block(
        self,
        body: list[ast.stmt],
        enclosing_tries: list[ast.Try],
        registered: set[str],
        out: list[tuple[str, int]],
        tail_following: list[ast.Try] | None = None,
    ) -> None:
        for i, stmt in enumerate(body):
            if isinstance(stmt, _FUNC):
                continue
            if _is_ensure_bucket(stmt):
                continue
            nxt = body[i + 1] if i + 1 < len(body) else None
            following = [nxt] if isinstance(nxt, ast.Try) and nxt.finalbody else []
            if nxt is None and tail_following:
                # The last statement of a `with` body: only __exit__ runs before the try
                # that follows the `with`, so that try protects it.
                following = tail_following
            for call in self._stmt_calls(stmt):
                kind = self.creates(call)
                if kind is None or kind in registered:
                    continue
                if kind in self.finally_removes(enclosing_tries + following):
                    continue
                out.append((kind, call.lineno))
            for field, child in ast.iter_fields(stmt):
                if field == "finalbody":
                    continue
                if isinstance(child, list) and child and isinstance(child[0], ast.stmt):
                    tries = enclosing_tries + ([stmt] if isinstance(stmt, ast.Try) else [])
                    if isinstance(stmt, ast.Try) and field == "body":
                        self._walk_block(child, tries, registered, out)
                    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                        self._walk_block(child, enclosing_tries, registered, out, following)
                    else:
                        self._walk_block(child, enclosing_tries, registered, out)

    @staticmethod
    def _stmt_calls(stmt: ast.stmt) -> list[ast.Call]:
        """Calls in the statement itself, not in its nested statement blocks."""
        out: list[ast.Call] = []
        for field, child in ast.iter_fields(stmt):
            if isinstance(child, list) and child and isinstance(child[0], ast.stmt):
                continue
            nodes = child if isinstance(child, list) else [child]
            for node in nodes:
                if isinstance(node, ast.AST):
                    if isinstance(node, ast.Call):
                        out.append(node)
                    out.extend(c for c in _own_calls(node) if isinstance(c, ast.Call))
        return out


#: Fixture parameters that ARE a throwaway Postgres server (see ``unprotected``).
_THROWAWAY_SERVER_PARAMS = frozenset(
    {"isolated_pg", "pg_container", "throwaway_pg", "shared_pg_container", "networked_pg"}
)


def _is_ensure_bucket(stmt: ast.stmt) -> bool:
    """``if not client.bucket_exists(b): client.make_bucket(b)`` makes sure the REAL bucket
    exists on a stack that lacks it; it is not a throwaway bucket and has no owner to undo it."""
    if not isinstance(stmt, ast.If):
        return False
    return any(
        isinstance(n, ast.Call) and _call_name(n) == "bucket_exists" for n in ast.walk(stmt.test)
    )


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, _FUNC)]


def _learn_helpers(
    trees: dict[str, ast.Module],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Fixpoint: helpers that remove a kind, and non-test helpers that create one and leave
    its removal to the caller (keyed ``stem.func``)."""
    creators: dict[str, str] = {}
    removers: dict[str, set[str]] = {}
    imports = {path: _imports(tree) for path, tree in trees.items()}
    changed = True
    while changed:
        changed = False
        for path, tree in trees.items():
            stem = Path(path).stem
            analyzer = _Analyzer(creators, removers, stem, imports[path])
            for fn in _functions(tree):
                kinds: set[str] = set()
                for call in _own_calls(fn):
                    kinds |= analyzer.removes(call)
                if kinds - removers.get(fn.name, set()):
                    removers[fn.name] = removers.get(fn.name, set()) | kinds
                    changed = True
                qualified = f"{stem}.{fn.name}"
                if _is_test_or_fixture(fn) or qualified in creators:
                    continue
                leaked = analyzer.unprotected(fn)
                if leaked:
                    creators[qualified] = leaked[0][0]
                    changed = True
    return creators, removers


#: A module that contains none of these cannot create (or remove) anything this guard
#: polices, so it is not parsed. Learned helper names are added to the filter as they are
#: found, so a module that only CALLS a creating helper is still scanned.
_KEYWORDS = (
    "indices.create",
    "_ensure_versioned_speaker_index",
    "put_pipeline",
    "make_bucket",
    "mkdtemp",
    "mkstemp",
    "NamedTemporaryFile",
    "CREATE DATABASE",
    '"docker"',
)


def scan(sources: dict[str, str]) -> list[Finding]:
    relevant = {p: s for p, s in sources.items() if any(k in s for k in _KEYWORDS)}
    trees = {path: ast.parse(src) for path, src in relevant.items()}
    while True:
        creators, removers = _learn_helpers(trees)
        names = {q.rsplit(".", 1)[-1] for q in creators}
        extra = {
            p: s
            for p, s in sources.items()
            if p not in trees and any(f"{name}(" in s for name in names)
        }
        if not extra:
            break
        trees.update({path: ast.parse(src) for path, src in extra.items()})
    findings: list[Finding] = []
    for path, tree in trees.items():
        analyzer = _Analyzer(creators, removers, Path(path).stem, _imports(tree))
        for fn in _functions(tree):
            if not _is_test_or_fixture(fn):
                continue
            for kind, lineno in analyzer.unprotected(fn):
                findings.append(Finding(path, fn.name, kind, lineno))
    return findings


@functools.lru_cache(maxsize=1)
def _tree_findings() -> tuple[Finding, ...]:
    return tuple(scan(_tree_sources()))


def _tree_sources() -> dict[str, str]:
    sources: dict[str, str] = {}
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        sources[str(path.relative_to(_REPO_ROOT))] = path.read_text(encoding="utf-8")
    return sources


# ---------------------------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_every_persistent_artifact_a_test_creates_is_removed_in_a_finally() -> None:
    findings = _tree_findings()
    offenders = sorted(
        f"{f.path}:{f.lineno} {f.function} ({f.kind})" for f in findings if f.key not in _ALLOWED
    )
    assert not offenders, (
        "a test/fixture creates a persistent artifact with no removal on its failure path "
        "(put the creation inside, or immediately before, a try whose finally removes it):\n"
        + "\n".join(offenders)
    )


@pytest.mark.unit
def test_every_allowlist_entry_is_still_needed_and_has_a_reason() -> None:
    live = {f.key for f in _tree_findings()}
    stale = sorted(k for k in _ALLOWED if k not in live)
    assert not stale, f"stale _ALLOWED entries (the finding is gone, delete them): {stale}"
    assert all(reason.strip() for reason in _ALLOWED.values())


@pytest.mark.unit
def test_the_scan_sees_real_creations_in_the_tree() -> None:
    """Not vacuous: the tree really creates every kind this guard claims to police, and the
    scan finds them (all protected). A detector that silently matched nothing would make the
    gate above pass on any tree."""
    trees = {
        path: ast.parse(src)
        for path, src in _tree_sources().items()
        if any(k in src for k in _KEYWORDS)
    }
    creators, _ = _learn_helpers(trees)
    seen: set[str] = set()
    for path, tree in trees.items():
        analyzer = _Analyzer(creators, {}, Path(path).stem, _imports(tree))
        for call in ast.walk(tree):
            if isinstance(call, ast.Call):
                kind = analyzer.creates(call)
                if kind:
                    seen.add(kind)
    assert set(KINDS) <= seen, f"kinds never seen in the tree: {set(KINDS) - seen}"


# ---------------------------------------------------------------------------------------------
# Guard the guard: must-fire and must-stay-clean, per kind and per accepted shape
# ---------------------------------------------------------------------------------------------

_FIRES = {
    "index-seeded-before-try": """
import pytest
@pytest.fixture
def idx(client):
    client.indices.create(index="x")
    client.bulk(body=[])
    try:
        yield "x"
    finally:
        client.indices.delete(index="x")
""",
    "index-happy-path-delete": """
def test_x(client):
    client.indices.create(index="x")
    assert client.count(index="x") == 0
    client.indices.delete(index="x")
""",
    "seeded-inside-the-with-after-create": """
def test_x(engine):
    with engine.connect() as conn:
        conn.execute('CREATE DATABASE "d"')
        conn.execute('SELECT 1')
    try:
        pass
    finally:
        conn.execute('DROP DATABASE "d"')
""",
    "pipeline-no-finally": """
def test_x(client):
    client.ingest.put_pipeline(id="p", body={})
""",
    "bucket-no-finally": """
def test_x(minio):
    minio.make_bucket("b")
    minio.put_object("b", "k", None, 0)
    minio.remove_bucket("b")
""",
    "database-no-finally": """
def test_x(conn):
    conn.execute('CREATE DATABASE "d"')
""",
    "docker-detached-no-finally": """
import subprocess
def test_x():
    subprocess.run(["docker", "run", "-d", "--name", "c", "img"])
""",
    "docker-network-no-finally": """
import subprocess
def test_x():
    subprocess.run(["docker", "network", "create", "n"])
""",
    "tmp-mkdtemp-never-removed": """
import tempfile
def test_x():
    d = tempfile.mkdtemp(prefix="p-")
    assert d
""",
    "tmp-namedtempfile-delete-false": """
import tempfile
def test_x():
    f = tempfile.NamedTemporaryFile(delete=False)
""",
    "helper-creates-caller-forgets": """
def _make(client):
    client.indices.create(index="x")
    return "x"
def test_x(client):
    name = _make(client)
    assert name
""",
}

_CLEAN = {
    "create-inside-try": """
def test_x(client):
    try:
        client.indices.create(index="x")
        client.bulk(body=[])
    finally:
        client.indices.delete(index="x", ignore=[404])
""",
    "create-immediately-before-try-yield": """
import pytest
@pytest.fixture
def idx(client):
    client.indices.create(index="x")
    try:
        client.bulk(body=[])
        yield "x"
    finally:
        client.indices.delete(index="x")
""",
    "removal-through-local-helper": """
def _drop(minio, b):
    for o in minio.list_objects(b):
        minio.remove_object(b, o)
    minio.remove_bucket(b)
def test_x(minio):
    minio.make_bucket("b")
    try:
        pass
    finally:
        _drop(minio, "b")
""",
    "addfinalizer-registered-first": """
import pytest, shutil, tempfile
@pytest.fixture
def d(request):
    path = tempfile.mkdtemp()
    request.addfinalizer(lambda: shutil.rmtree(path))
    return path
""",
    "helper-creates-caller-protects": """
def _make(client):
    client.indices.create(index="x")
    return "x"
def test_x(client):
    name = _make(client)
    try:
        assert name
    finally:
        client.indices.delete(index=name)
""",
    "created-at-the-end-of-a-with-before-try": """
def test_x(engine):
    with engine.connect() as conn:
        conn.execute('CREATE DATABASE "d"')
    try:
        pass
    finally:
        with engine.connect() as conn:
            conn.execute('DROP DATABASE "d"')
""",
    "docker-foreground-rm": """
import subprocess
def test_x():
    subprocess.run(["docker", "run", "--rm", "img", "true"])
""",
    "database-dropped-in-finally": """
def test_x(conn):
    conn.execute('CREATE DATABASE "d"')
    try:
        pass
    finally:
        conn.execute('DROP DATABASE IF EXISTS "d"')
""",
    "docker-removed-in-finally": """
import subprocess
def test_x():
    subprocess.run(["docker", "run", "-d", "--name", "c", "img"])
    try:
        pass
    finally:
        subprocess.run(["docker", "rm", "-f", "c"])
""",
}


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(_FIRES))
def test_the_guard_fires(case: str) -> None:
    assert scan({f"{case}.py": _FIRES[case]}), f"{case}: a leak the guard must report"


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(_CLEAN))
def test_the_guard_stays_clean(case: str) -> None:
    assert scan({f"{case}.py": _CLEAN[case]}) == [], f"{case}: an accepted shape was reported"


@pytest.mark.unit
def test_every_kind_has_a_must_fire_case() -> None:
    fired = {f.kind for src in _FIRES.values() for f in scan({"case.py": src})}
    assert fired == set(KINDS), f"kinds with no must-fire case: {set(KINDS) - fired}"
