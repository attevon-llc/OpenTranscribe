"""The Locust load scenario calls only real routes, covers the API, and cleans up after itself.

``scripts/perf/load/`` is run by hand against an isolated stack, so nothing else would notice a
renamed route: the scenario would just report 404s as "load". This imports it against inert
stand-ins for ``locust``, ``gevent`` and ``websocket`` (``tests/fixtures/locust_stub.py``; the
real packages patch the standard library and must never load here), runs every persona's tasks
with a recording client, and checks each request against ``backend/openapi.json``.
"""

from __future__ import annotations

import io
import wave
from collections import Counter
from collections import defaultdict
from typing import Any

import pytest

from tests.fixtures import locust_stub as stub

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@pytest.fixture
def load(monkeypatch):
    return stub.load_modules(monkeypatch)


@pytest.fixture
def run(load):
    return stub.run_personas(load["locustfile"])


@pytest.fixture(scope="module")
def spec():
    return stub.openapi()


def _route_key(call: stub.Call) -> str:
    return call.name.split(" ")[0]


# --- every request is a real route ----------------------------------------------------------


def test_every_request_is_a_real_route_with_its_required_query_parameters(load, run, spec):
    routes = load["routes"]
    unknown, missing = set(), set()
    for call in run.client.calls:
        found = stub.operation_for(call.method, call.url, spec)
        if found is None or found[0] != routes.ROUTES[_route_key(call)][1]:
            # also a literal path the router would hand to a templated one
            unknown.add(f"{call.method} {call.url} -> {found and found[0]}")
            continue
        required = {
            p["name"]
            for p in found[1].get("parameters", [])
            if p["in"] == "query" and p.get("required")
        }
        if required - set(call.params):
            missing.add(f"{call.method} {found[0]} lacks {sorted(required - set(call.params))}")

    assert run.client.calls
    assert not unknown, f"not in backend/openapi.json: {sorted(unknown)}"
    assert not missing, sorted(missing)


def test_every_declared_route_exists_and_is_exercised(load, run, spec):
    routes = load["routes"]
    for key, (method, template) in routes.ROUTES.items():
        assert method.lower() in spec["paths"].get(template, {}), (key, method, template)
    used = {_route_key(call) for call in run.client.calls}

    assert used == set(routes.ROUTES), set(routes.ROUTES) ^ used


def test_the_socket_path_is_the_apps_websocket_route(load):
    from starlette.routing import WebSocketRoute

    from app.main import app

    paths = {route.path for route in app.routes if isinstance(route, WebSocketRoute)}

    assert load["routes"].WS_PATH in paths, paths


# --- every API group is covered by a persona, or excluded with a reason ---------------------


def _groups(spec: dict[str, Any]) -> dict[str, str]:
    """``"METHOD template" -> first OpenAPI tag`` for every operation."""
    return {
        f"{method.upper()} {template}": (op.get("tags") or ["-"])[0]
        for template, operations in spec["paths"].items()
        for method, op in operations.items()
    }


def coverage_problems(
    all_groups: set[str],
    observed: dict[str, set[str]],
    declared: dict[str, tuple[str, ...]],
    excluded: dict[str, str],
) -> list[str]:
    """What is wrong with the coverage table, empty when nothing is."""
    problems = []
    for group in sorted(all_groups - set(observed) - set(excluded)):
        problems.append(f"{group}: no persona calls it and it is not excluded")
    for group in sorted(set(observed) & set(excluded)):
        problems.append(f"{group}: excluded but a persona calls it")
    for group, reason in excluded.items():
        if not reason.strip():
            problems.append(f"{group}: excluded without a reason")
        if group not in all_groups:
            problems.append(f"{group}: excluded but not an OpenAPI group (stale)")
    as_declared = {group: set(personas) for group, personas in declared.items()}
    if as_declared != observed:
        problems.append(f"GROUP_PERSONAS differs from the recorded run: {observed}")
    return problems


def _observed(run: stub.Run, spec: dict[str, Any]) -> dict[str, set[str]]:
    groups = _groups(spec)
    observed: dict[str, set[str]] = defaultdict(set)
    for call in run.client.calls:
        template = stub.operation_for(call.method, call.url, spec)
        assert template is not None
        observed[groups[f"{call.method} {template[0]}"]].add(call.persona)
    return dict(observed)


def test_every_api_group_is_covered_by_a_persona_or_excluded_with_a_reason(load, run, spec):
    routes = load["routes"]
    all_groups = set(_groups(spec).values())

    problems = coverage_problems(
        all_groups, _observed(run, spec), routes.GROUP_PERSONAS, routes.EXCLUDED_GROUPS
    )

    assert not problems, problems


def test_the_coverage_check_reports_a_new_group_and_a_stale_exclusion(load, run, spec):
    routes = load["routes"]
    all_groups = set(_groups(spec).values()) | {"brand-new-group"}
    excluded = {**routes.EXCLUDED_GROUPS, "files": "covered, so this is wrong", "gone": "x"}

    problems = coverage_problems(all_groups, _observed(run, spec), routes.GROUP_PERSONAS, excluded)

    assert "brand-new-group: no persona calls it and it is not excluded" in problems
    assert "files: excluded but a persona calls it" in problems
    assert "gone: excluded but not an OpenAPI group (stale)" in problems


# --- writes: only with the write tag, and always undone -------------------------------------


def test_a_read_only_run_makes_no_write(load):
    run = stub.run_personas(load["locustfile"], exclude_tags=frozenset({"write"}), rounds=10)
    writes = {
        f"{c.persona}.{c.task}: {c.method} {c.url}"
        for c in run.client.calls
        if c.method in WRITE_METHODS
        and c.url != "/api/auth/login"
        and c.persona != "PipelineUploader"
    }

    assert any(c.persona == "Editor" for c in run.client.calls)
    assert not writes, sorted(writes)


def test_only_the_gpu_tagged_tasks_send_a_recording_to_the_pipeline(load):
    web = tuple(n for n in load["locustfile"].USER_CLASSES if n != "PipelineUploader")
    without_gpu = stub.run_personas(
        load["locustfile"], exclude_tags=frozenset({"gpu"}), personas=web, rounds=10
    )
    with_gpu = stub.run_personas(load["locustfile"], personas=web, rounds=10)

    def uploads(run: stub.Run) -> int:
        return sum(1 for c in run.client.calls if (c.method, c.url) == ("POST", "/api/files"))

    assert uploads(without_gpu) == 0
    assert uploads(with_gpu) > 0


def test_writes_clean_up_after_themselves(run):
    by_route = Counter(_route_key(c) for c in run.client.calls)
    labels = Counter(c.name for c in run.client.calls)
    pairs = [
        ("comments.create", "comments.delete"),
        ("tags.add_to_file", "tags.delete"),
        ("collections.create", "collections.delete"),
        ("collections.add_media", "collections.remove_media"),
        ("speakers.create", "speakers.merge"),
        ("files.hide", "files.unhide"),
        ("files.prepare", "files.purge"),
        ("files.upload", "files.purge real upload"),
        ("llm.create", "llm.delete"),
        ("chat.create", "chat.delete"),
    ]

    for made, undone in pairs:
        count = labels[undone] if " " in undone else by_route[undone]
        assert by_route[made] > 0, made
        assert by_route[made] == count, (made, by_route[made], undone, count)
    # every segment edit and every speaker reassignment is followed by its restore
    assert labels["segments.update restore"] == labels["segments.update"] > 0
    assert labels["segments.speaker"] > 0


def test_an_upload_is_purged_only_after_the_pipeline_finished_it(run):
    seen: list[str] = []
    for call in run.client.calls:
        if call.persona != "PipelineUploader":
            continue
        seen.append(_route_key(call))
    first = seen.index("files.upload")

    assert seen[first : first + 4] == [
        "files.upload",
        "files.status_detail",
        "files.delete",
        "files.purge",
    ]
    assert any(f.kind == "PIPELINE" and f.error is None for f in run.fired)


# --- streams, sockets and the gated personas ------------------------------------------------


def test_a_chat_turn_records_the_first_token_and_the_whole_answer(run):
    names = {(f.kind, f.name) for f in run.fired if f.error is None}

    assert ("SSE", "mock-gpt first token") in names
    assert ("SSE", "mock-gpt whole answer") in names
    assert ("SSE", "mock-slow whole answer") in names


def test_the_listener_holds_an_authenticated_socket_and_times_the_echo(load, run):
    sockets = stub.StubWebsocket.sockets
    fired = {(f.kind, f.name) for f in run.fired if f.error is None}

    assert sockets, "the listener never connected"
    assert sockets[0].url == "ws://localhost:5474" + load["routes"].WS_PATH
    assert sockets[0].kwargs["cookie"] == "access_token=jwt"
    assert sockets[0].kwargs["origin"] == stub.HOST
    assert ("WS", "connect") in fired
    assert ("WS", "echo round trip") in fired
    assert ("WS", "notification file_status") in fired


def test_the_org_admin_stops_when_the_edition_has_no_org_pages(load):
    run = stub.run_personas(load["locustfile"], capabilities={}, personas=("OrgAdmin",), rounds=2)

    assert run.stopped == ["OrgAdmin"]
    assert not [c for c in run.client.calls if c.url.startswith("/api/org-admin")]


# --- identities, the sample recording and the target guard -----------------------------------


def test_identity_blocks_are_disjoint_and_cover_every_ordinary_user(load):
    scenario = load["scenario"]
    seeded = 60
    blocks = [scenario.pool_bounds(name, seeded) for name, _ in scenario.POOL_SHARES]
    covered = [i for lo, hi in blocks for i in range(lo, hi + 1)]

    assert covered == list(range(scenario.FIRST_POOL_INDEX, seeded + 1))
    assert all(lo <= hi for lo, hi in blocks)
    with pytest.raises(ValueError, match="at least"):
        scenario.pool_bounds("viewer", scenario.MIN_SEEDED_USERS - 1)


def test_each_upload_is_a_distinct_valid_recording(load):
    scenario = load["scenario"]
    audio = scenario.SAMPLE_AUDIO.read_bytes()
    first, second = scenario.unique_wav(audio, "a1"), scenario.unique_wav(audio, "b22")

    def frames(data: bytes) -> bytes:
        with wave.open(io.BytesIO(data)) as w:
            pcm: bytes = w.readframes(w.getnframes())
        return pcm

    assert first != second != audio
    assert frames(first) == frames(second) == frames(audio)


@pytest.mark.parametrize(
    ("offset", "host", "expected"),
    [
        (None, None, "missing"),
        ("0", None, "share the dev ports"),
        ("abc", None, "share the dev ports"),
        ("300", "http://localhost:5174", "is not the"),
        ("300", None, None),
        ("300", "http://localhost:5474", None),
    ],
)
def test_the_target_must_be_a_fresh_stack(load, tmp_path, offset, host, expected):
    scenario = load["scenario"]
    if offset is not None:
        (tmp_path / ".fresh").mkdir()
        (tmp_path / ".fresh" / "perf1337.offset").write_text(offset + "\n")
    if expected is None:
        target = scenario.resolve_target("perf1337", host, repo_root=tmp_path, backend_port="5474")
        assert target.base_url == "http://localhost:5474"
        assert target.compose_project == "otfresh-perf1337"
    else:
        with pytest.raises(scenario.NotIsolatedError, match=expected):
            scenario.resolve_target("perf1337", host, repo_root=tmp_path, backend_port="5474")


def test_the_port_is_the_observed_one_never_derived_from_the_offset(load, tmp_path, monkeypatch):
    scenario = load["scenario"]
    (tmp_path / ".fresh").mkdir()
    (tmp_path / ".fresh" / "perf1337.offset").write_text("300\n")
    monkeypatch.setenv("BACKEND_PORT", "5999")
    assert (
        scenario.resolve_target("perf1337", None, repo_root=tmp_path).base_url
        == "http://localhost:5999"
    )
    monkeypatch.delenv("BACKEND_PORT")
    with pytest.raises(scenario.NotIsolatedError, match="BACKEND_PORT is not exported"):
        scenario.resolve_target("perf1337", None, repo_root=tmp_path)
    with pytest.raises(scenario.NotIsolatedError, match="shared dev stack"):
        scenario.resolve_target("perf1337", None, repo_root=tmp_path, backend_port="5174")


def test_no_fresh_name_is_refused(load):
    with pytest.raises(load["scenario"].NotIsolatedError, match="--fresh NAME is required"):
        load["scenario"].resolve_target("", None)


# --- M2 tool bugs T4, T6, T8 and the unfiltered speaker list (#1174) -------------------------


def test_a_204_from_topic_suggestions_is_not_a_failure(load):
    # T4: the endpoint answers 204 when it has nothing; the stub raises on any unexpected status
    run = stub.run_personas(
        load["locustfile"],
        rounds=3,
        personas=("Viewer",),
        statuses={r"/api/files/[^/]+/suggestions": 204},
    )

    assert any(c.url.endswith("/suggestions") for c in run.client.calls)


def test_the_speaker_merge_is_the_restore_and_names_the_original(run):
    # T6: moving the segment back first deleted the temp speaker, so the merge 404ed
    calls = [c for c in run.client.calls if c.persona == "Editor"]
    names = [c.name for c in calls]
    merges = [c for c in calls if _route_key(c) == "speakers.merge"]

    assert merges
    assert "segments.speaker restore" not in names
    assert all(c.url.endswith("/merge/spk-1") for c in merges)
    first_move = names.index("segments.speaker")
    assert names.index("speakers.merge", first_move) > first_move


def test_the_viewer_drives_the_unfiltered_paged_speaker_list(run):
    # M2 #14
    paged = [c for c in run.client.calls if c.name == "speakers.list paged unfiltered"]

    assert paged
    assert paged[0].params == {"skip": 0, "limit": 100}


def test_the_listener_ending_its_own_run_is_not_a_dropped_socket(load, run):
    # T8: Locust closing the socket at run end raised a read error counted as `WS dropped`
    listener = load["ws_listener"].WebSocketListener.__new__(load["ws_listener"].WebSocketListener)
    fired: list[tuple] = []
    listener._fire = lambda kind, name, *a, **k: fired.append((kind, name))
    listener.pending = {}

    class Closed:
        def recv(self):
            raise stub.SocketError("closed")

        def close(self):
            pass

    listener.ws = Closed()
    listener._stopping = True
    listener._receive_once()
    assert fired == []

    listener.ws = Closed()
    listener._stopping = False
    listener._receive_once()
    assert fired == [("WS", "dropped")]
