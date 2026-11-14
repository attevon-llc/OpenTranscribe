# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
import atexit
import itertools
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if TYPE_CHECKING:
    from tests.testrun_marker import LiveRunMarker

# git exports GIT_DIR and the other repository-local variables to every hook (githooks(5):
# "git sets GIT_DIR ... environment variables"). A test that runs `git init`/`git add` in a scratch
# dir from inside a pre-commit/pre-push hook would otherwise act on the REAL repository.
# The list comes from `git rev-parse --local-env-vars` (git-rev-parse(1)); the fallback is the
# same list as printed by git 2.43.
_GIT_LOCAL_ENV_FALLBACK = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


def _git_local_env_vars() -> tuple[str, ...]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return _GIT_LOCAL_ENV_FALLBACK
    return tuple(out.split()) or _GIT_LOCAL_ENV_FALLBACK


for _git_var in _git_local_env_vars():
    os.environ.pop(_git_var, None)

# Shell tests run opentr.sh from the checkout root; without this it links the main checkout's
# .env into a worktree and leaves the link behind (see tests/unit/test_opentr_dry_run_and_identity.py).
os.environ.setdefault("OPENTR_NO_ENV_LINK", "1")

# Add backend directory to Python path for imports
_backend_dir = Path(__file__).parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

# Read database credentials from .env file without loading all variables
# This avoids polluting the environment with variables that aren't in Settings
_project_root = _backend_dir.parent
_env_file = _project_root / ".env"
_env_values = {}
if _env_file.exists():
    _env_values = dotenv_values(_env_file)

# Set only the service credentials we need from .env (DB always; MinIO for the
# S3-backed tests that activate when the dev stack is reachable). Explicitly
# exported values win over .env so CI / throwaway-DB runs can override.
_db_vars = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
_minio_vars = ["MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "MEDIA_BUCKET_NAME"]
for _var in _db_vars + _minio_vars:
    if _var in _env_values and _env_values[_var]:
        os.environ.setdefault(_var, _env_values[_var])

# Create temporary directories for testing. One per process (each xdist worker imports this
# file), removed when the process exits. It used to be removed by nothing: 7,358 of these were
# sitting in /tmp on 2026-10-11. A SIGKILLed worker still skips atexit; the prefix is
# registered in scripts/cleanup-test-data.py's tmp plane, which sweeps what that leaves.
_test_temp_dir = tempfile.mkdtemp(prefix="opentranscribe_test_")
atexit.register(shutil.rmtree, _test_temp_dir, ignore_errors=True)
_test_data_dir = os.path.join(_test_temp_dir, "data")
_test_upload_dir = os.path.join(_test_data_dir, "uploads")
_test_models_dir = os.path.join(_test_temp_dir, "models")
_test_temp_subdir = os.path.join(_test_temp_dir, "temp")

# Create all directories
for _dir in [_test_data_dir, _test_upload_dir, _test_models_dir, _test_temp_subdir]:
    os.makedirs(_dir, exist_ok=True)


def _service_reachable(host: str, port: int, timeout: float = 0.3) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _observed_stack_ports() -> dict[str, str]:
    """Ports the resolved stack publishes (tests/stack_preflight.py), loaded by path because this
    runs before any package path is set up. Only worth the docker call when a port is unset."""
    needed = ("POSTGRES_PORT", "MINIO_PORT", "OPENSEARCH_PORT")
    if all(os.environ.get(v) not in (None, "", "5432") for v in needed):
        return {}
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_ot_stack_preflight", Path(__file__).with_name("stack_preflight.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observed: dict[str, str] = module.resolved_port_env()
    return observed


# Set testing environment flag and disable external services in tests
os.environ["TESTING"] = "True"
# Declare the suite as a relaxed environment. ENVIRONMENT defaults to "production"
# and fails closed (#284 A0.3), which would otherwise put Secure=True on session
# cookies — and the TestClient talks plain http://testserver, so it would silently
# drop them and every cookie-auth test would see an anonymous session.
os.environ["ENVIRONMENT"] = "testing"
os.environ["SKIP_CELERY"] = "True"
os.environ["SKIP_REDIS"] = "True"
# SKIP_REDIS is advisory only; the memory-path tests assume nothing answers the default
# redis://localhost:6379. A foreign Redis on a shared host flipped 27 of them (v0.7.0 gate).
# Pin a refused port unless a phase exported REDIS_* (integration/GPU/e2e: REDIS_ENV in
# scripts/run-integration-tests.sh), where REDIS_URL would override their HOST/PORT.
if not any(os.environ.get(_v) for _v in ("REDIS_URL", "REDIS_HOST", "REDIS_PORT")):
    os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
os.environ["SKIP_WEBSOCKET"] = "True"
os.environ["RATE_LIMIT_ENABLED"] = "false"  # Disable rate limiting for tests

# Auto-detect MinIO and OpenSearch from the dev stack so the gated S3/search tests
# run when the services are up and skip when they aren't (e.g. bare CI runners).
# An explicit SKIP_S3 / SKIP_OPENSEARCH in the shell always wins over detection.
#
# Probe the endpoint the tests will ACTUALLY use, resolved from the environment first.
# The probe used to be hard-coded to the dev stack's 5178/5180 while the client honoured
# MINIO_PORT/OPENSEARCH_PORT, so detection and use could disagree. Against an isolated
# `--fresh --port-offset` stack that had two failure modes, both silent:
#   * forget to export MINIO_PORT and it probed 5178 and *used* 5178 — live storage paired
#     with the throwaway Postgres, which is exactly the mixed-stack state --fresh exists to
#     prevent;
#   * export it and the enable/disable decision still came from the *live* stack, so with
#     the fresh MinIO down the S3 suites ran against an unreachable port and failed
#     confusingly instead of skipping.
# Deriving both from one value removes the disagreement and lets `--port-offset` work.
# POSTGRES_PORT below has always behaved this way; this brings MinIO/OpenSearch in line.
_MINIO_PROBE_HOST = os.environ.get("MINIO_HOST", "localhost")
_OBSERVED = _observed_stack_ports()
_MINIO_PROBE_PORT = int(os.environ.get("MINIO_PORT") or _OBSERVED.get("MINIO_PORT", "0"))
_OPENSEARCH_PROBE_HOST = os.environ.get("OPENSEARCH_HOST", "localhost")
_OPENSEARCH_PROBE_PORT = int(
    os.environ.get("OPENSEARCH_PORT") or _OBSERVED.get("OPENSEARCH_PORT", "0")
)

os.environ.setdefault(
    "SKIP_S3", "False" if _service_reachable(_MINIO_PROBE_HOST, _MINIO_PROBE_PORT) else "True"
)
os.environ.setdefault(
    "SKIP_OPENSEARCH",
    "False" if _service_reachable(_OPENSEARCH_PROBE_HOST, _OPENSEARCH_PROBE_PORT) else "True",
)
if os.environ["SKIP_S3"] == "False":
    # The dev stack maps the MinIO S3 API to localhost:5178 (console is 5179).
    os.environ["MINIO_HOST"] = _MINIO_PROBE_HOST
    os.environ["MINIO_PORT"] = str(_MINIO_PROBE_PORT)
if os.environ["SKIP_OPENSEARCH"] == "False":
    # The dev stack exposes OpenSearch on localhost:5180 (not the in-cluster 9200).
    os.environ["OPENSEARCH_HOST"] = _OPENSEARCH_PROBE_HOST
    os.environ["OPENSEARCH_PORT"] = str(_OPENSEARCH_PROBE_PORT)
# NOTE on Celery dispatch in tests: endpoints that .delay() a task (e.g.
# PUT /speakers/{uuid}) used to publish into whatever Redis answered on the
# host's default localhost:6379 — an unrelated container on this machine —
# because SKIP_CELERY never covered the dispatch path and the stack's real
# Redis (localhost:5177) requires auth. That worked by accident. The honest
# fix is the autouse _skip_celery_dispatch fixture below, which no-ops
# Task.apply_async (and therefore every .delay) whenever SKIP_CELERY is set.
# The audit logger has its own OpenSearch switch (app/auth/audit.py). Always off
# in unit tests: savepoint rollback cannot undo OpenSearch writes, so leaving it
# on would pollute the live dev audit index with thousands of test login events.
os.environ.setdefault("AUDIT_LOG_TO_OPENSEARCH", "false")

# Set paths to use temporary directories for testing (must be set before importing config)
os.environ["DATA_DIR"] = _test_data_dir
os.environ["MODELS_DIR"] = _test_models_dir
os.environ["TEMP_DIR"] = _test_temp_subdir

# Database connection for local testing - use localhost instead of Docker service name
# The .env file has POSTGRES_HOST=postgres (Docker service) but we need localhost
os.environ["POSTGRES_HOST"] = "localhost"
# The port is the OBSERVED one of the resolved stack (scripts/lib/stack_target.py), not the stock
# 5176: that default silently pointed a worktree or --fresh run at the live database. Unresolvable
# (no docker, no single stack) leaves it unset and the connection fails loudly.
if os.environ.get("POSTGRES_PORT") in (None, "", "5432") and "POSTGRES_PORT" in _OBSERVED:
    os.environ["POSTGRES_PORT"] = _OBSERVED["POSTGRES_PORT"]

# --- CUDA device guard (issue #719) -----------------------------------------
# The fast suite opened a ~300 MiB CUDA primary context on EVERY xdist worker
# that happened to import torch, on whichever device is default (device 0) —
# multiplied by `-n auto`, that is tens of contexts on cards this project does
# not own (GPU 0/2 are reserved for unrelated work; root CLAUDE.md). Nothing in
# the harness restricted device visibility before this.
#
# WHY THIS LIVES IN `pytest_configure`, NOT AT MODULE LEVEL:
# `import torch` is not the freeze point — the first CUDA *call* is. Setting
# CUDA_VISIBLE_DEVICES after even one `torch.cuda.is_available()` leaves
# `is_available()` True while `device_count()` reads 0: an inconsistent state
# WORSE than no guard, because `hardware_detection._detect_optimal_device` then
# takes the cuda branch and calls `torch.cuda.set_device(0)` against a device
# that doesn't exist. `pytest_configure` runs before torch is ever imported in
# every invocation shape checked (fast suite, `-m gpu`, `-m integration`, an
# e2e run via `import tests.conftest`).
#
# WHY A HOOK AND NEVER A MODULE-LEVEL ASSIGNMENT:
# `tests/e2e/conftest.py` does `import tests.conftest` purely for its module
# body's side effects — the e2e run has its OWN pytest.ini/rootdir, so hooks
# defined here do NOT register for it. A module-level `os.environ[...] = ...`
# would therefore blind every e2e run with no hook available to ever release
# it. Putting the guard in `pytest_configure` means e2e is untouched by
# construction: the module body runs, the hook does not.
_CUDA_GUARD_SENTINEL = "-1"
_CUDA_GUARD_HATCH = "OT_TEST_CUDA_VISIBLE_DEVICES"
_CUDA_GUARD_APPLIED = pytest.StashKey[bool]()

# pytest expression grammar keywords that are never marker names.
_MARKEXPR_KEYWORDS = {"not", "and", "or", "True", "False", "None"}


def _selection_may_run_gpu_tests(markexpr: str) -> bool:
    """Return whether some marker set containing "gpu" satisfies `markexpr`.

    The default markexpr is `not integration and not gpu`, which CONTAINS the
    substring "gpu" — a naive `if "gpu" in markexpr` would disable the guard on
    every default run, invisibly. Two files are marked BOTH `integration` and
    `gpu` (`test_diar_native_smoke_live.py`, `test_gpu_scale_smoke_live.py`), so
    the gate's `-m integration` phase legitimately selects gpu tests and must
    NOT be blinded either. Satisfiability — is there an assignment of the
    expression's own marker names (forcing gpu=True) that evaluates True — is
    the only predicate that gets both cases right.
    """
    if not markexpr.strip():
        # An empty markexpr selects everything, including `gpu` tests. Blinding
        # here would silently turn a `gpu`-marked test into a "CUDA not
        # available" skip — a green vacuum. pytest_runtest_setup below turns
        # that into a loud, non-skippable failure instead of relying on this
        # function to somehow return True for "everything".
        return False
    names = sorted((set(re.findall(r"[A-Za-z_]\w*", markexpr)) - _MARKEXPR_KEYWORDS) | {"gpu"})
    if len(names) > 12:
        # Combinatorial guard: 2**12 = 4096 evaluations, comfortably fast. A
        # markexpr naming more than 12 distinct markers is not realistic here;
        # refuse rather than hang.
        return False
    try:
        from _pytest.mark.expression import Expression  # private API
    except ImportError:
        # Fail toward the SAFE direction: if we cannot ask pytest what the
        # expression means, assume it might select gpu tests and leave CUDA
        # visible. test_the_marker_predicate_rejects_the_default_expression's
        # fallback case (7.5) pins this so it can never regress silently.
        return True
    expr = Expression.compile(markexpr)
    for combo in itertools.product((False, True), repeat=len(names)):
        env = dict(zip(names, combo, strict=True))
        if not env["gpu"]:
            continue
        if expr.evaluate(lambda n, _env=env: _env.get(n, False)):
            return True
    return False


_LIVE_RUN_MARKER: "pytest.StashKey[LiveRunMarker]" = pytest.StashKey()


def _register_live_test_run(config: pytest.Config) -> None:
    """Mark this pytest run LIVE for ``scripts/cleanup-test-data.py`` (see testrun_marker.py).

    The controller process only: xdist workers live and die inside it, so its marker already
    covers whatever they create, and N workers would otherwise mean N markers per run.
    """
    if hasattr(config, "workerinput"):
        return
    from tests.testrun_marker import LiveRunMarker
    from tests.testrun_marker import default_testruns_dir

    config.stash[_LIVE_RUN_MARKER] = LiveRunMarker(default_testruns_dir(_project_root))


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """D2: do not spawn ``-n auto`` (one worker per core) for a handful of named test files.

    Worker start-up and per-worker collection dominate a one-file run. Defers (returns None) to
    ``PYTEST_XDIST_AUTO_NUM_WORKERS`` when set, since this hook outranks that variable
    (pytest-xdist "Running tests across multiple CPUs" docs), and to xdist's default whenever a
    directory is given or there are more than four files.
    """
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    args = [a for a in config.args if not a.startswith("-")]
    if not args:
        return None
    files = {a.split("::", 1)[0] for a in args}
    if any(not f.endswith(".py") for f in files) or len(files) > 4:
        return None
    return max(1, len(files))


def pytest_sessionstart(session: pytest.Session) -> None:
    """Refuse a stack whose schema is not this checkout's head (controller only; see
    ``tests/stack_preflight.py``). Override with ``ST_ALLOW='schema-head=<reason>'``."""
    if hasattr(session.config, "workerinput"):
        return
    from tests.stack_preflight import EXIT_CODE
    from tests.stack_preflight import run_preflight

    problem = run_preflight()
    if problem:
        pytest.exit(problem, returncode=EXIT_CODE)


def pytest_unconfigure(config: pytest.Config) -> None:
    marker = config.stash.get(_LIVE_RUN_MARKER, None)
    if marker is not None:
        marker.release()


def pytest_configure(config: pytest.Config) -> None:
    """Restrict CUDA device visibility unless the selection may run gpu tests.

    Escape hatch `OT_TEST_CUDA_VISIBLE_DEVICES`: "1" sets CUDA_VISIBLE_DEVICES
    and skips the marker check entirely; "all" leaves it exactly as inherited
    from the parent shell; "" is an explicit blind; unset means the marker
    predicate above decides.

    Deliberately does NOT use `os.environ.setdefault` (unlike lines 39/95/98/120
    in this same file) — an ambient CUDA_VISIBLE_DEVICES already set in the
    developer's shell must NOT release the guard. `setdefault` would make that
    the single most likely way this ships broken; test 7.7
    (`test_an_ambient_cuda_visible_devices_does_not_release_the_guard`) exists
    to catch a regression back to it.
    """
    _register_live_test_run(config)
    hatch = os.environ.get(_CUDA_GUARD_HATCH)
    if hatch is not None:
        if hatch != "all":
            os.environ["CUDA_VISIBLE_DEVICES"] = hatch
        config.stash[_CUDA_GUARD_APPLIED] = False
        return

    markexpr = config.getoption("markexpr", default="") or ""
    if _selection_may_run_gpu_tests(markexpr):
        config.stash[_CUDA_GUARD_APPLIED] = False
        return

    os.environ["CUDA_VISIBLE_DEVICES"] = _CUDA_GUARD_SENTINEL
    # PCI_BUS_ID makes pytest's/torch's device ordinals agree with `nvidia-smi`,
    # GPU_DEVICE_ID and Docker `device_ids`. Measured on this host: CUDA's
    # default FASTEST_FIRST ordering does NOT match nvidia-smi (nvidia-smi
    # 0=A6000/1=RTX3080Ti/2=A6000, torch default order 0=A6000/1=A6000/
    # 2=RTX3080Ti) — so under the default ordering, CUDA_VISIBLE_DEVICES=1
    # would select nvidia-smi GPU 2, a card reserved for unrelated work. This
    # is a real behaviour change on multi-GPU hosts, applied here rather than
    # left implicit.
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    config.stash[_CUDA_GUARD_APPLIED] = True


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Hard-fail a `gpu`-marked test collected under the guard, never skip it.

    Closes the empty-markexpr hole loudly: `-m ''` (or no `-m` at all) selects
    everything including `gpu` tests, which `_selection_may_run_gpu_tests`
    treats as blind by design. Without this, a `gpu` test run that way would
    SKIP on "CUDA not available" — a green vacuum, this repo's signature
    failure mode (issue #431). A per-item `pytest.fail(pytrace=False)` is used
    rather than a session-level UsageError in `pytest_collection_modifyitems`:
    a UsageError raised inside an xdist worker can break the collection
    protocol, per the measured plan for issue #719.
    """
    if not item.config.stash.get(_CUDA_GUARD_APPLIED, False):
        return
    if item.get_closest_marker("gpu") is None:
        return
    pytest.fail(
        "cuda-device-guard: this test is marked @pytest.mark.gpu but CUDA "
        "devices are hidden (CUDA_VISIBLE_DEVICES="
        f"{os.environ.get('CUDA_VISIBLE_DEVICES')!r}). Select it explicitly "
        "with `-m gpu` (or set OT_TEST_CUDA_VISIBLE_DEVICES) rather than "
        "letting it run blind — a skip here would look like a pass.",
        pytrace=False,
    )


# --- end CUDA device guard --------------------------------------------------

# Import app modules after setting environment variables (they check env during import)
from app.core.config import settings

# Use PostgreSQL test database (same as main database but with test schema isolation)
# Models use PostgreSQL-specific types (JSONB) that SQLite doesn't support
SQLALCHEMY_TEST_DATABASE_URL = settings.DATABASE_URL
engine = create_engine(SQLALCHEMY_TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Shared fixture modules, registered here so every suite sees them without a per-file import.
#
# NOTE: this line is why `tests/` must NOT gain an `__init__.py`. With one,
# prepend import mode roots `tests/conftest.py` at `backend/` instead of
# `backend/tests/`, so `backend/tests` never reaches sys.path and this dies with
# `ImportError: Error importing plugin "fixtures.mock_llm": No module named
# 'fixtures'` — the same reason `--import-mode=importlib` is unusable here.
#
# `fixtures.dir_collector_memo` contributes no fixtures at all: it is the
# workaround for the pytest 9.1 regression that makes every subdirectory
# conftest's fixtures vanish from a mixed file selection (issue #454). Read its
# docstring before touching it; `unit/test_conftest_fixture_visibility.py` pins
# both the workaround and the fact that pytest still needs it.
pytest_plugins = [
    "fixtures.mock_llm",
    "fixtures.mock_asr",
    "fixtures.dir_collector_memo",
    "fixtures.search_corpus_stack",
    "fixtures.auth_state_isolation",
    # `open_registration`: opt-in fixture that opens self-registration in the test savepoint.
    "fixtures.open_registration",
    "fixtures.production_txn",
    # `tracking_env`: detect_speaker_attributes_task with a real DB session and everything slow faked.
    "fixtures.speaker_attribute_tracking",
    # `summary_plane` + `sync_summary_plane`: seed the OpenSearch summary plane (#963) from
    # Postgres for API-level summary-search tests. Opt-in per module; nothing autouse.
    "fixtures.summary_plane",
    # `availability_indices`: throwaway chunks/transcripts/speaker indices plus the indexing and
    # read-surface helpers the trash and quarantine lifecycle suites share (#1101, #1253).
    # Opt-in; nothing autouse.
    "fixtures.availability_index_world",
    # `support_world`: two organizations, an org admin, a platform admin and a super_admin with
    # real tokens, for the support-access grant suites (#1122). Opt-in; nothing autouse.
    "fixtures.support_world",
    # `destroyed` + `make_file`/`trash`/`exists`: a recording external destroy and a trashable file
    # for the trash purge and retention suites (#1101). Opt-in; nothing autouse.
    "fixtures.trash_world",
    # `gov`: one organization (admin, two members, an outsider) plus builders for files,
    # collections, shares and holds, and `acting_as` to run requests as one of them, for the
    # content-governance suites (#1275-#1277). Opt-in; nothing autouse.
    "fixtures.governance_world",
    # `speaker_os_seams`: recording stand-ins for every OpenSearch seam the shared speaker
    # cleanup (#1214) touches. Opt-in; nothing autouse.
    "fixtures.speaker_cleanup_seams",
    # `speaker_assignment_seams`: the kNN reads the assignment paths make (cluster and
    # profile matching, re-cluster embeddings), answered from the same recorders (#1233).
    "fixtures.speaker_assignment_seams",
    # `hf_cache`: an empty temporary Hugging Face hub cache, with builders for complete and
    # interrupted repository layouts (#1246). Opt-in; nothing autouse.
    "fixtures.hf_cache",
    # `mock_hf_hub`: a loopback stand-in for the Hugging Face hub (model-info API + file
    # endpoint with ETag/Range) so the real downloader runs with no network (#1246).
    "fixtures.mock_hf_hub",
    # `local_model_redis`: one throwaway redis:7-alpine per xdist worker on a free loopback
    # port, for the model-download lock/state/supervisor/endpoint suites (#1246). Opt-in.
    "fixtures.throwaway_redis",
    # `local_model_redis`: one throwaway redis:7-alpine per xdist worker on a free loopback
    # port, for the model-download lock/state/supervisor/endpoint suites (#1246). Opt-in.
    "fixtures.throwaway_redis",
    # `migration_state_store`: an in-memory Redis for the migration lock and progress records, so
    # the migration endpoints (which refuse to start when Redis is unreadable) can be exercised.
    # Opt-in; nothing autouse.
    "fixtures.migration_state_store",
    # `document_export`: runs the DOCX/PDF transcript export end to end in process (route, then
    # the Celery task body) with storage and Redis recorded, so a test reads the bytes a user
    # would download (#1278). Opt-in; nothing autouse.
    "fixtures.document_export",
    # `privacy_world` / `privacy_allows_all`: the real privacy policy (#1280) read from the
    # test's own rows, and its opt-out for suites that pass fake file ids. Opt-in.
    "fixtures.privacy_world",
    # `resident_pii_analyzer`: the startup warm-up a deployment runs, so a test driving live
    # detection through an endpoint is not refused a request-thread model build. Opt-in.
    "fixtures.resident_detectors",
    # pytest's own bundled plugin, enabling the `pytester` fixture — used by
    # unit/test_e2e_base_url_precedence.py to run a real, isolated pytest subprocess
    # against a copy of tests/e2e/pytest.ini (issue #965). It contributes no fixtures
    # of its own beyond `pytester`/`pytester_static_path`, so nothing else changes.
    "pytester",
]


@pytest.fixture(autouse=True, scope="session")
def _skip_celery_dispatch():
    """No-op every Celery dispatch when SKIP_CELERY is set.

    SKIP_CELERY historically only skipped worker wiring — endpoints calling
    ``task.delay(...)`` still published to the broker. On a developer host that
    silently targeted whatever Redis answered on localhost:6379 (an unrelated
    container), and against the real stack Redis (localhost:5177, auth'd) it
    500s. Patching ``Task.apply_async`` (which ``.delay`` wraps) and the app's
    ``celery_app.send_task`` (dispatch by task name, which never goes through a
    ``Task``) makes the skip-switch actually cover dispatch, with a fake
    AsyncResult id for code that records task ids. Without the second patch a
    by-name dispatch spent ~19 s retrying an unreachable broker per call. The
    instance, not the ``Celery`` class: the live broker suites build Celery apps
    of their own and must keep a real ``send_task``.
    """
    if os.environ.get("SKIP_CELERY") != "True":
        yield
        return
    from unittest.mock import MagicMock
    from unittest.mock import patch

    fake_result = MagicMock(name="FakeAsyncResult")
    fake_result.id = "test-task-id"
    from app.core.celery import celery_app

    with (
        patch("celery.app.task.Task.apply_async", return_value=fake_result),
        patch.object(celery_app, "send_task", return_value=fake_result),
    ):
        yield


@pytest.fixture
def auth_mail_dispatches(monkeypatch):
    """Record every ``auth_mail.*`` task dispatch without running or publishing it.

    Returns the list ``[(task_name, args, kwargs), ...]``. The password-reset,
    verification, invitation and security-notice mail all leave the request through these
    tasks, so what a request dispatched is what it did about mail.
    """
    from types import SimpleNamespace

    from app.core.celery import celery_app

    calls: list[tuple[str, tuple, dict]] = []

    def fake_send_task(name, args=None, kwargs=None, **_options):
        if name.startswith("auth_mail."):
            calls.append((name, tuple(args or ()), dict(kwargs or {})))
        return SimpleNamespace(id="auth-mail-test-task")

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    return calls


@pytest.fixture
def run_auth_mail_inline(monkeypatch, db_session):
    """Run ``auth_mail.*`` tasks in-process at dispatch time (other tasks stay no-ops).

    For round-trip tests that need the mail the task builds, e.g. to read the single-use
    token out of it. Retries run eagerly, without the backoff delay. The tasks share the
    test's own session, because the request under test has not committed its rows to any
    other connection.
    """
    import contextlib
    import importlib

    from app.core.celery import celery_app

    importlib.import_module("app.tasks.auth_mail_tasks")  # registers the tasks on celery_app

    @contextlib.contextmanager
    def shared_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.tasks.auth_mail_tasks.session_scope", shared_scope)

    other = celery_app.send_task

    def send_task(name, args=None, kwargs=None, **options):
        if name.startswith("auth_mail."):
            return celery_app.tasks[name].apply(args=args, kwargs=kwargs, throw=True)
        return other(name, args, kwargs, **options)

    monkeypatch.setattr(celery_app, "send_task", send_task)


@pytest.fixture(autouse=True)
def _no_host_default_gateway(monkeypatch):
    """Give header trust an empty routing table: the test host's gateway is not a test input.

    ``app.auth.header_trust.default_gateways`` refuses this namespace's default gateway as an
    identity proxy. On a developer machine or a CI runner that is the host's router (10.0.0.1,
    192.168.1.1, ...), the same literals the trust tests use as proxy addresses, so a test would
    pass or fail by where it ran. Tests of the gateway rule pass their own table.
    """
    from app.auth import header_trust

    monkeypatch.setattr(header_trust, "ROUTE_TABLE", Path(os.devnull))


@pytest.fixture(autouse=True)
def _release_burst_connections():
    """Close the idle connections a concurrent test left in the shared app engine's pool.

    ``QueuePool`` keeps up to ``DB_POOL_SIZE`` (20) returned connections open, so a test that
    fans out N threads through ``SessionLocal`` (a multi-slice summary, a reservation race)
    leaves N server connections pinned to its xdist worker for the rest of the session. The
    floor is already two per worker (this module's engine and the app's), 96 of the throwaway
    database's 100 under ``-n auto`` on 48 cores, so one such burst failed unrelated modules
    with "too many clients already" (af11 sweep). ``dispose()`` closes only the checked-in
    connections; one still checked out is unaffected.
    """
    yield
    from sqlalchemy.pool import QueuePool

    from app.db.base import engine as app_engine

    pool = app_engine.pool
    if isinstance(pool, QueuePool) and pool.checkedin() > 1:
        app_engine.dispose()


@pytest.fixture(autouse=True)
def _clear_process_auth_cache():
    """Isolate the process-wide auth-config cache between tests.

    ``app.core.process_auth_settings`` caches effective auth config for the whole
    process, and under ``TESTING`` a cache generation never ages out (see
    ``_process_cache_ttl``) — deliberately, so a value primed from the test's own
    savepointed session survives the test. That makes clearing it here the thing
    that keeps one test's ``account_lockout_threshold=3`` out of the next test.
    """
    from app.core.process_auth_settings import clear_process_auth_settings_cache

    clear_process_auth_settings_cache()
    yield
    clear_process_auth_settings_cache()


@pytest.fixture(autouse=True)
def _forget_last_banner_read(monkeypatch):
    """Start each test with no remembered banner setting.

    ``lifecycle_gates._last_banner_read`` is deliberately process-global: after a failed
    settings read the gate falls back to the last value read. Any test that reads an
    enabled banner therefore leaves ``(True, ...)`` behind, and a later test whose fake
    session cannot answer the query (``test_websocket_auth_accepts_active_user_with_valid_token``)
    was refused as "banner not acknowledged" on whichever xdist worker ran it second.
    """
    import app.api.endpoints.auth.lifecycle_gates as gates

    monkeypatch.setattr(gates, "_last_banner_read", None)
    yield


@pytest.fixture(autouse=True)
def _unpin_opensearch_client_reexport():
    """Keep ``app.services.opensearch_service.opensearch_client`` forwarding to the client module.

    The package resolves that name through a module ``__getattr__`` (it is rebound at run
    time). ``monkeypatch.setattr`` on it records the forwarded value and, on undo, SETS it as
    a real package attribute, which then shadows ``__getattr__`` for the rest of the worker.
    A test that patched the client module first and the package second therefore left its
    fake client pinned there, and a later test reading the package got a ``MagicMock``
    (``test_owner_listing_tenancy::test_reindex_status_counts_only_the_active_tenant`` after
    ``test_voiceprint_document_stamps_by_writer``). Removing the pinned entry after every test
    restores the forwarding, whichever test pinned it.

    It also runs BEFORE the test: ``monkeypatch`` is created by an earlier autouse fixture, so
    its undo runs after this teardown and re-pins the forwarded value of a package-level
    ``setattr`` once the post-test removal has already happened. Clearing on entry catches that
    pin before the next test reads it (``test_opensearch_client_unpin_after_monkeypatch``).
    """
    import app.services.opensearch_service as package

    vars(package).pop("opensearch_client", None)
    yield
    vars(package).pop("opensearch_client", None)


@pytest.fixture(autouse=True)
def _clear_sidecar_readiness_cache():
    """Isolate the diar-native readiness/liveness TTL caches between tests.

    Same hazard as ``_clear_process_auth_cache`` above, for the same reason: ``_ready_cache``
    and ``_status_cache`` in ``app.transcription.diar_sidecar_status`` are module-level dicts with
    a multi-second TTL, so a verdict cached by one test is visible to every later test in the
    same xdist worker. ``reset_readiness_cache()`` already existed but was called only by the
    handful of tests that flip a server's state mid-test — everything else inherited whatever
    the previous test left behind.

    That is not theoretical. `TestOverlapMidJobFailureDegradesSafely::
    test_release_and_fallback_happen_on_the_main_thread_after_transcription` failed a full
    gate run on `manager.release_calls == 1` while passing in isolation AND in a 13,019-test
    suite run of the same command — the signature of order-dependent state, not of a defect in
    the code under test. The caches are keyed by URL, and the tests in that module stand up
    real servers on `_free_port()` values that the OS is free to hand out again.

    Clearing costs two dict `.clear()` calls per test. Leaving it uncleared costs a failure
    that reproduces only under a particular test ordering, which is the most expensive kind.
    """
    from tests.state_resets import reset_readiness_cache

    reset_readiness_cache()
    yield
    reset_readiness_cache()


#: The DDL isolation lock is defined in `tests/db_locks.py` so that this fixture and the
#: tests that open their own DB connection share ONE definition of the key. A second copy
#: of the literal would stop protecting anything the moment either copy changed.
from tests.db_locks import acquire_ddl_lock_exclusive
from tests.db_locks import acquire_ddl_lock_shared


@pytest.fixture
def no_receipt_measurement():
    """Opt-in: stand in for the erasure receipt's measurement stage.

    For tests that pin what an erasure DESTROYS in the SQL plane. The receipt measures every
    store of the inventory (OpenSearch, object storage, Redis, the filesystem) from a fresh
    session, so on a shared stack its outcome depends on whatever other runs have left in those
    stores -- a stray ``test_*`` index alone makes it ``unverified`` (``os.unclassified``) and
    the erasure, correctly, refuses to complete. The receipt itself is covered against real
    stores by ``integration/test_erasure_receipt_e2e.py``.
    """
    from unittest.mock import MagicMock
    from unittest.mock import patch

    with (
        patch(
            "app.services.gdpr_erasure_service.receipt_flow.begin",
            return_value=(MagicMock(), {}),
        ),
        patch("app.services.gdpr_erasure_service.receipt_flow.finish"),
    ):
        yield


@pytest.fixture
def no_erasure_extras():
    """Opt-in: stand in for the erasure's object-storage / Redis / filesystem destroy stage.

    ``erase_extras`` calls the default storage client (5 retries, ~5 minute timeouts); with no
    MinIO reachable (CI, ``SKIP_S3``) each erasure burns ~18-54 s in retry backoff. For tests that
    pin what the SQL plane destroys. Covered against real stores by
    ``integration/test_erasure_receipt_e2e.py``.
    """
    from unittest.mock import patch

    with patch("app.services.gdpr_erasure_service.erase_extras"):
        yield


@pytest.fixture(scope="function")
def db_session(request):
    """Fixture that provides a SQLAlchemy session for tests.

    Uses nested transactions (savepoints) for test isolation - all changes made during
    a test are rolled back at the end, leaving the database in its original state.

    This handles the case where the code under test calls commit() by using
    a savepoint that can be rolled back.

    Tests marked ``@pytest.mark.ddl_exclusive`` run schema DDL (``DROP TABLE`` /
    ``ALTER TABLE ... DROP CONSTRAINT``) that takes Postgres's ``ACCESS EXCLUSIVE``
    lock. That lock is not confined to the table named in the statement: dropping a
    table (or constraint) with a foreign key also drops the FK's enforcement trigger
    on the *referenced* table, which needs an ``ACCESS EXCLUSIVE`` lock there too
    (issue #389 — dropping `scim_token` this way also locks `user`, since
    `scim_token.created_by` references it). Nearly every other test creates or
    touches a `user` row, so under `-n auto` a `ddl_exclusive` test can deadlock
    against an unrelated worker's ordinary DML — a cross-table lock-wait cycle, not
    just a collision with another DDL test. The existing `xdist_group("migration_ddl")`
    marker only keeps DDL tests off each other on one worker; it does nothing about
    every OTHER worker's unmarked tests running at the same wall-clock moment.
    A Postgres advisory lock gives real mutual exclusion across every xdist worker
    (they all talk to the same Postgres instance): every ordinary test takes the
    lock's SHARED form for the life of its transaction; a `ddl_exclusive` test takes
    the EXCLUSIVE form, which blocks until every in-flight ordinary test finishes and
    blocks every new one from starting until the DDL transaction ends (commit or
    rollback — `pg_advisory_xact_lock*` auto-releases either way, so a failed test
    can't leave the suite wedged).

    Apply ``ddl_exclusive`` to the **individual tests that execute DDL, never to a
    module**. Every EXCLUSIVE acquisition is a stop-the-world barrier: it drains all
    other workers and queues every new one behind it, so a read-only schema assertion
    that carries the marker costs a full drain for nothing. Module-scope application is
    what made the `migration_ddl` group 414 s of a 511 s wall clock — 111 tests marked,
    ~12 actually running DDL (issue #431). `tests/unit/test_ddl_marker_discipline.py`
    enforces both directions and fails on either mistake.
    """
    from sqlalchemy import event

    connection = engine.connect()
    transaction = connection.begin()

    if request.node.get_closest_marker("ddl_exclusive") is not None:
        # Also sets lock_timeout, so a genuine cross-connection cycle fails loudly instead
        # of hanging. This lock only covers connections that opt in; ~20 app paths open
        # their own `SessionLocal()` during a TestClient request
        # (app/db/session_utils.py, app/utils/prompt_manager.py, …) and are invisible to it.
        acquire_ddl_lock_exclusive(connection)
    else:
        acquire_ddl_lock_shared(connection)

    # Create a session bound to this connection
    session = TestingSessionLocal(bind=connection)

    # Start a nested transaction (savepoint)
    session.begin_nested()

    # When the session commits, restart the nested transaction
    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(session, trans):
        if trans.nested and not trans._parent.nested:
            # The savepoint was committed, start a new one
            session.begin_nested()

    try:
        yield session
    finally:
        # Remove the event listener
        event.remove(session, "after_transaction_end", restart_savepoint)
        session.close()
        # Rollback the outer transaction to undo all test changes
        try:
            if transaction.is_active:
                transaction.rollback()
        except Exception:
            pass
        connection.close()


@pytest.fixture(scope="function")
def client(db_session):
    """Fixture that provides a FastAPI TestClient with test DB session"""
    # Imported here, not at module level: only a test that requests `client` pays the app import.
    from app.db.base import get_db
    from app.main import app

    # Override the get_db dependency to use test DB session
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    # Create test client
    with TestClient(app) as test_client:
        yield test_client

    # Remove only our override (race-safe for parallel workers)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(scope="function")
def normal_user(db_session):
    """Fixture that creates a normal user in the test database.

    Uses a unique UUID-based email to avoid conflicts between parallel tests.
    """
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"testuser_{unique_id}@example.com",
        full_name="Test User",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def admin_user(db_session):
    """Fixture that creates an admin user in the test database.

    Uses a unique UUID-based email to avoid conflicts between parallel tests.
    """
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"testadmin_{unique_id}@example.com",
        full_name="Admin User",
        hashed_password=get_password_hash("adminpass"),
        is_active=True,
        # is_superuser mirrors (role == super_admin); an 'admin' is not a superuser.
        # Use the super_admin_user fixture for super_admin-tier endpoints.
        is_superuser=False,
        role="admin",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def user_token_headers(client, normal_user):
    """Fixture that returns auth headers for a regular user."""
    # Using form-encoded data for OAuth2 password flow
    response = client.post(
        "/api/auth/token",
        data={"username": normal_user.email, "password": "password123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, f"Login failed for {normal_user.email}: {response.json()}"
    tokens = response.json()
    access_token = tokens["access_token"]
    # Return headers along with the user email for tests that need to verify it
    headers = {"Authorization": f"Bearer {access_token}"}
    headers["_test_user_email"] = normal_user.email  # Metadata for tests
    return headers


@pytest.fixture(scope="function")
def admin_token_headers(client, admin_user):
    """Fixture that returns auth headers for an admin user."""
    # Using form-encoded data for OAuth2 password flow
    response = client.post(
        "/api/auth/token",
        data={"username": admin_user.email, "password": "adminpass"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, f"Login failed for {admin_user.email}: {response.json()}"
    tokens = response.json()
    access_token = tokens["access_token"]
    # Return headers along with the user email for tests that need to verify it
    headers = {"Authorization": f"Bearer {access_token}"}
    headers["_test_user_email"] = admin_user.email  # Metadata for tests
    return headers


@pytest.fixture(scope="function")
def super_admin_user(db_session):
    """Fixture that creates a super_admin user in the test database.

    Uses a unique UUID-based email to avoid conflicts between parallel tests.
    """
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"testsuperadmin_{unique_id}@example.com",
        full_name="Super Admin User",
        hashed_password=get_password_hash("superadminpass"),
        is_active=True,
        is_superuser=True,
        role="super_admin",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def super_admin_token_headers(client, super_admin_user):
    """Fixture that returns auth headers for a super_admin user."""
    response = client.post(
        "/api/auth/token",
        data={"username": super_admin_user.email, "password": "superadminpass"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, (
        f"Login failed for {super_admin_user.email}: {response.json()}"
    )
    tokens = response.json()
    access_token = tokens["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    headers["_test_user_email"] = super_admin_user.email
    return headers


@pytest.fixture(scope="session")
def sample_wav_bytes() -> bytes:
    """A minimal valid PCM WAV file that passes magic-byte upload validation.

    0.1 s of 16 kHz mono silence (~3.2 KB). Generated with the stdlib so tests
    never depend on fixture files or external downloads.
    """
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


@pytest.fixture
def upload_test_file(client, sample_wav_bytes):
    """Factory that uploads a real WAV via the API and cleans up MinIO afterwards.

    The DB row rolls back with the savepoint, but the MinIO object does not —
    the finalizer deletes the file through the API so the dev bucket stays clean.
    """
    import io

    uploaded: list[tuple[str, dict]] = []

    def _upload(headers: dict, filename: str = "test_audio.wav") -> dict:
        files = {"file": (filename, io.BytesIO(sample_wav_bytes), "audio/wav")}
        response = client.post("/api/files", headers=headers, files=files)
        assert response.status_code == 200, f"File upload failed: {response.json()}"
        data: dict = response.json()
        uploaded.append((str(data.get("uuid") or data.get("id")), headers))
        return data

    yield _upload

    for file_id, headers in uploaded:
        try:
            client.delete(f"/api/files/{file_id}", headers=headers)
        except Exception:
            pass  # DB rollback removes the row; MinIO orphans are best-effort


# --- Fixture aliases for test_media_security.py and other tests ---


@pytest.fixture(scope="function")
def sample_user(normal_user):
    """Alias for normal_user fixture."""
    return normal_user


@pytest.fixture(scope="function")
def auth_headers(user_token_headers):
    """Alias for user_token_headers fixture."""
    return user_token_headers


@pytest.fixture(scope="function")
def admin_auth_headers(admin_token_headers):
    """Alias for admin_token_headers fixture."""
    return admin_token_headers


@pytest.fixture(scope="function")
def other_user(db_session):
    """Create a second normal user for access-control tests."""
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"otheruser_{unique_id}@example.com",
        full_name="Other User",
        hashed_password=get_password_hash("otherpass123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def other_user_auth_headers(client, other_user):
    """Auth headers for the other_user (second normal user)."""
    response = client.post(
        "/api/auth/token",
        data={"username": other_user.email, "password": "otherpass123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, f"Login failed for {other_user.email}: {response.json()}"
    tokens = response.json()
    access_token = tokens["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    headers["_test_user_email"] = other_user.email
    return headers
