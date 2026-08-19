"""Every worker that splits sentences must be able to load punkt (issue #436).

`chunk_transcript_by_speaker_turns` splits with NLTK punkt when a punkt model is
resolvable and with a regex otherwise. The two splitters cut in different places
— measured over the eval corpus: **49 files, 226 chunks differ in text** — so
which one answers is a property of the *process*, not of the code.

The `nltk_data` mount existed on `backend`, `celery-worker` and the GPU workers,
and on **neither of the two workers that actually index**: `index_transcript_search`
runs on the `embedding` queue and `reindex_transcripts` on the `cpu` queue. Every
chunk in the live index was therefore cut by the regex fallback, while every test
process and the host venv resolve punkt — so the tests exercised a code path
production never took, and `--dispatch eager` (host, punkt) and `--dispatch celery`
(worker, regex) built *different indexes from the same corpus*. That silently
undermines #403 D5, where every stage is a delta against the previous one.

The test derives both halves rather than asserting a hand-written list of
services:

* which **tasks** reach the splitter — from the source, by following the call
  graph from `split_into_sentences` outward (:func:`_splitter_dependent_modules`);
* which **queue** each task runs on — from `celery_app.conf.task_routes`;
* which **service** serves that queue — from each compose service's `-Q` flag.

So a task moved to another queue, a queue moved to another service, or a third
caller of the splitter all keep the assertion honest instead of dating it.
:func:`test_the_call_graph_walk_finds_the_known_callers` is the guard on the
guard: a walk that silently matched nothing would pass everything.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.yml"
APP = REPO_ROOT / "backend" / "app"

#: Overlays that redefine a splitter-dependent worker and must therefore keep the
#: mount. This read ONLY the base file (issue #491 item 11) — and
#: ``docker-compose.offline.yml``, the overlay an airgapped install relies on most,
#: had the WORST coverage of any compose file: it re-declared `volumes:` on
#: `celery-cpu-worker`, `celery-nlp-worker` and `celery-embedding-worker` without
#: `nltk_data`, and a re-declared `volumes:` list REPLACES the base one per service.
#: Checking one file could not see that.
OVERLAYS = ("docker-compose.prod.yml", "docker-compose.offline.yml")

#: The mount every splitter-dependent worker needs, by container SUFFIX.
#:
#: Matched on the tail rather than the full path: the images do not agree on the
#: home directory — `Dockerfile.blackwell` runs as `user`, so its cache is
#: `/home/user/.cache/nltk_data` — and hardcoding `/home/appuser` made every
#: Blackwell mount invisible to this test while looking thorough.
NLTK_CACHE_SUFFIX = ".cache/nltk_data"

#: Seed of the call-graph walk: the only sentence splitter in the app.
SPLITTER_FUNCTION = "split_into_sentences"

#: Task modules the walk is expected to reach. Not the assertion — the *guard*
#: on the walk (see the module docstring). Losing one of these means the walk
#: broke, not that the bug is fixed.
EXPECTED_TASK_MODULES = {
    "app/tasks/search_indexing_task.py",  # index_transcript_search  (embedding)
    "app/tasks/reindex_task.py",  # reindex_transcripts     (cpu)
    "app/tasks/ingest_artifacts_task.py",  # artifacts.generate_file_facts (nlp)
}


def _module_key(path: Path) -> str:
    return path.relative_to(REPO_ROOT / "backend").as_posix()


@cache
def _python_sources() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in APP.rglob("*.py"):
        try:
            trees[_module_key(path)] = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - a parse error is a real failure
            pytest.fail(f"Could not parse {path}: {exc}")
    return trees


def _called_names(node: ast.AST) -> set[str]:
    """Every bare name and attribute tail that appears in a call position."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _functions_in(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function and method, keyed by the name a caller writes.

    Methods are keyed by their bare name because that is what the call site says
    (`indexing_service.reindex_transcript(...)`) — the receiver's type is not
    recoverable from the AST, and a name collision only widens the walk.
    """
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[node.name] = node
    return found


def _imported_modules(tree: ast.Module, module_key: str) -> set[str]:
    """Module keys this module imports from, in any import form.

    Relative imports are resolved against *module_key*'s package — the search
    package imports its own siblings that way (`from .chunking_service import …`),
    which is precisely the first hop of the chain being followed.
    """
    package = module_key[: -len(".py")].split("/")[:-1]
    keys: set[str] = set()
    for node in ast.walk(tree):
        dotted: list[str] = []
        if isinstance(node, ast.Import):
            dotted = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = ".".join(package[: len(package) - node.level + 1]) if node.level else ""
            base = ".".join(part for part in (prefix, node.module or "") if part)
            if base:
                # `from a.b import c` reaches both a/b.py and a/b/c.py.
                dotted = [base] + [f"{base}.{alias.name}" for alias in node.names]
        for name in dotted:
            if name.startswith("app."):
                keys.add(name.replace(".", "/") + ".py")
    return keys


@cache
def _splitter_dependent_functions() -> set[tuple[str, str]]:
    """``(module_key, function_name)`` pairs that reach :data:`SPLITTER_FUNCTION`.

    Function-level, not module-level: `file_cleanup_service` calls
    `delete_transcript_chunks` on the same class that owns
    `index_transcript_chunks`, and a module-granular walk therefore concludes
    that deleting a file chunks a transcript. Edges resolve a call only into a
    module the caller imports (or into its own module), so a shared method name
    cannot pull in an unrelated package.
    """
    trees = _python_sources()
    functions = {key: _functions_in(tree) for key, tree in trees.items()}
    calls = {
        (key, name): _called_names(node)
        for key, defs in functions.items()
        for name, node in defs.items()
    }
    imports = {key: _imported_modules(tree, key) for key, tree in trees.items()}

    tainted = {node for node, names in calls.items() if SPLITTER_FUNCTION in names}
    changed = True
    while changed:
        changed = False
        for (key, name), names in calls.items():
            if (key, name) in tainted:
                continue
            visible = (imports[key] & set(functions)) | {key}
            if any((origin, called) in tainted for origin in visible for called in names):
                tainted.add((key, name))
                changed = True
    return tainted


def _splitter_dependent_modules() -> set[str]:
    """Modules with at least one splitter-dependent function."""
    return {key for key, _ in _splitter_dependent_functions()}


def _chunking_task_names() -> dict[str, str]:
    """Celery task name -> the module it lives in, for tasks that reach the splitter."""
    tainted = _splitter_dependent_functions()
    trees = _python_sources()
    tasks: dict[str, str] = {}
    for module_key, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or (module_key, node.name) not in tainted:
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                for keyword in decorator.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        tasks[str(keyword.value.value)] = module_key
    return tasks


def _queue_for_task(task_name: str) -> str:
    from app.core.celery import celery_app

    routes = celery_app.conf.task_routes or {}
    route = routes.get(task_name)
    assert route is not None, f"{task_name!r} has no explicit route — which worker runs it?"
    return str(route["queue"])


def _compose_services(path: Path | None = None) -> dict[str, dict]:
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose file")
    document = yaml.safe_load((path or COMPOSE).read_text(encoding="utf-8"))
    return dict(document.get("services") or {})


def _queues_served_by(service: dict) -> set[str]:
    command = service.get("command")
    if not isinstance(command, str):
        return set()
    match = re.search(r"-Q\s+([\w,\-]+)", command)
    if not match:
        return set()
    return {part for part in match.group(1).split(",") if part}


#: `${VAR:-default}` contains a colon of its own, so interpolation is removed
#: before a `source:target[:mode]` mount is split. Splitting first silently
#: yields "-./models}/nltk_data" as the target and every mount looks absent.
_INTERPOLATION = re.compile(r"\$\{[^}]*\}")


def _mount_targets(service: dict) -> set[str]:
    targets: set[str] = set()
    for volume in service.get("volumes") or []:
        if not isinstance(volume, str):
            continue
        parts = _INTERPOLATION.sub("", volume).split(":")
        if len(parts) >= 2:
            targets.add(parts[1].split("#")[0].strip())
    return targets


def test_the_call_graph_walk_finds_the_known_callers() -> None:
    """Guard on the guard: a walk matching nothing would pass every assertion."""
    tainted = _splitter_dependent_modules()
    assert "app/services/search/chunking_service.py" in tainted, (
        "The splitter's own module is not tainted — the walk is not walking."
    )
    reached = set(_chunking_task_names().values())
    missing = EXPECTED_TASK_MODULES - reached
    assert not missing, (
        f"The call-graph walk no longer reaches a chunking task in {sorted(missing)}. "
        f"Either the task stopped chunking (update EXPECTED_TASK_MODULES) or the walk broke."
    )
    assert "app/services/file_cleanup_service.py" not in tainted, (
        "Deleting a file's chunks does not chunk anything — the walk has gone "
        "module-granular again and now taints every caller of the indexing service."
    )


def test_every_queue_that_chunks_has_a_worker_with_punkt() -> None:
    """The #436 assertion: derive queue -> service -> mount, end to end."""
    tasks = _chunking_task_names()
    assert tasks, "No splitter-dependent Celery task found at all."

    queues: dict[str, str] = {}
    for task_name in sorted(tasks):
        queues[_queue_for_task(task_name)] = task_name

    services = _compose_services()
    unserved: list[str] = []
    unmounted: list[str] = []
    for queue, task_name in sorted(queues.items()):
        serving = [name for name, svc in services.items() if queue in _queues_served_by(svc)]
        if not serving:
            unserved.append(f"{queue} (runs {task_name})")
            continue
        for name in serving:
            if not any(t.endswith(NLTK_CACHE_SUFFIX) for t in _mount_targets(services[name])):
                unmounted.append(f"{name} (queue {queue}, runs {task_name})")

    assert not unserved, f"No compose service serves these chunking queues: {unserved}"
    assert not unmounted, (
        f"These workers chunk transcripts with no *{NLTK_CACHE_SUFFIX} mount, so they cut "
        f"sentences with the regex fallback while every other process uses punkt "
        f"(issue #436): {unmounted}"
    )


@pytest.mark.parametrize("overlay_name", OVERLAYS)
def test_an_overlay_resolves_nltk_data_to_one_source(overlay_name: str) -> None:
    """Every worker's ``nltk_data`` must resolve to the SAME host directory.

    ⚠️ **Not "the overlay must re-declare the mount".** This test was first written
    on the premise that a re-declared ``volumes:`` REPLACES the base list per
    service — which is what issue #491 item 6 asserts. Measured with
    ``docker compose config``, that is **false**: compose MERGES ``volumes`` across
    files by target, so the overlay omitting ``nltk_data`` never removed it, and
    every worker had the mount all along. The first version of this test duly
    reported a false positive against ``docker-compose.prod.yml``.

    What IS real is item 7, and it is worse than the issue describes. The overlay
    defaulted ``MODEL_CACHE_DIR`` to ``/opt/opentranscribe/models`` while the base
    file defaults to ``./models``, and the merge keeps whichever file declared the
    target — so with the variable unset, one deployment had:

        celery-worker      /home/appuser/.cache/nltk_data  <- /opt/opentranscribe/models/nltk_data
        celery-cpu-worker  /home/appuser/.cache/nltk_data  <- ./models/nltk_data

    Same target, different source, in the same ``docker compose up``.
    ``download-models.sh`` populates one of them, so the other worker sees an empty
    directory and silently falls back to the regex splitter — the #436 defect
    again, reached through the default rather than through a missing line.

    ⚠️ This is invisible unless ``MODEL_CACHE_DIR`` is genuinely unset. The
    repo's own ``.env`` sets it, which makes both halves agree and hides the
    divergence — the first measurement here was confounded exactly that way.
    """
    overlay_path = REPO_ROOT / overlay_name
    if not overlay_path.is_file():
        pytest.skip(f"{overlay_name} is not present in this checkout")

    base = _compose_services()
    overlay = _compose_services(overlay_path)

    sources: dict[str, set[str]] = {}
    for name in set(base) | set(overlay):
        for service in (base.get(name), overlay.get(name)):
            if not service:
                continue
            for volume in service.get("volumes") or []:
                if not isinstance(volume, str) or "nltk_data" not in volume:
                    continue
                # Everything before the FIRST `/nltk_data` is the host root. A
                # `:` split is wrong here — `${VAR:-default}` contains one — and
                # an rsplit takes the CONTAINER path's occurrence instead.
                sources.setdefault(name, set()).add(volume.split("/nltk_data", 1)[0])

    distinct = {source for values in sources.values() for source in values}
    assert len(distinct) <= 1, (
        f"{overlay_name} and the base file resolve nltk_data to DIFFERENT host "
        f"directories, so whichever one download-models.sh populates leaves the "
        f"others reading an empty cache: {sorted(distinct)}"
    )


def test_the_source_check_can_actually_fail() -> None:
    """Guard on the guard: the assertion above passes trivially once aligned.

    Without this, a predicate that silently collected nothing would also report
    "<= 1 distinct source" and pass forever.
    """
    diverged = {
        "a": {"${MODEL_CACHE_DIR:-./models}"},
        "b": {"${MODEL_CACHE_DIR:-/opt/opentranscribe/models}"},
    }
    distinct = {source for values in diverged.values() for source in values}
    assert len(distinct) > 1, "the predicate cannot tell two different defaults apart"


def test_the_mount_suffix_predicate_matches_a_non_appuser_home() -> None:
    """``Dockerfile.blackwell`` runs as ``user``, not ``appuser``.

    Hardcoding ``/home/appuser`` made every Blackwell mount invisible to this
    module while it looked thorough (issue #491 item 11).
    """
    blackwell = {"volumes": ["${MODEL_CACHE_DIR:-./models}/nltk_data:/home/user/.cache/nltk_data"]}
    assert any(t.endswith(NLTK_CACHE_SUFFIX) for t in _mount_targets(blackwell))

    without = {"volumes": ["${MODEL_CACHE_DIR:-./models}/torch:/home/appuser/.cache/torch"]}
    assert not any(t.endswith(NLTK_CACHE_SUFFIX) for t in _mount_targets(without))
