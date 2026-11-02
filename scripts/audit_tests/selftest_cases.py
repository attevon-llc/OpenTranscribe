"""Must-fire and must-stay-clean fixtures for the self-test."""

from __future__ import annotations

# ------------------------------------------------------------------------------- self-test

#: ``(category, source)`` — each source MUST produce the named category. A detector that
#: silently stops matching reports 0 findings, which is indistinguishable from a clean
#: suite. That is the same sin this script exists to catch, so it audits itself.
SELFTEST_CASES: tuple[tuple[str, str], ...] = (
    (
        'permissive-status',
        'def test_a(client):\n    r = client.get("/x")\n    assert r.status_code in (200, 403)\n',
    ),
    (
        'negated-status',
        'def test_a(client):\n    r = client.get("/x")\n    assert r.status_code != 403, r.text\n',
    ),
    (
        'status-guarded-assert',
        'def test_a(client):\n'
        '    r = client.get("/x")\n'
        '    assert r.status_code == 200\n'
        '    if r.status_code == 200:\n'
        '        assert len(r.json()) == 3\n',
    ),
    (
        'conditional-only',
        'def test_a(feature):\n    if feature:\n        assert feature.value == 1\n',
    ),
    (
        'conditional-skip',
        'def test_a(feature):\n'
        '    if feature:\n'
        '        assert feature.value == 1\n'
        '    else:\n'
        '        pytest.skip("no feature")\n',
    ),
    (
        'loop-only',
        'def test_a(detector):\n'
        '    spans = detector.detect(text)\n'
        '    for s in spans:\n'
        '        assert 0 <= s.start < s.end\n',
    ),
    (
        # A dynamic INNER loop makes the body vacuous even when the outer iterable
        # is a literal — the shape nine test_search_quality tests are written in.
        'loop-only',
        'def test_a(client):\n'
        '    for q in ["china", "fraud"]:\n'
        '        data = client.get(f"/search?q={q}").json()\n'
        '        for r in data["results"]:\n'
        '            assert r["score"] > 0\n',
    ),
    (
        # A name REBOUND from a runtime value is not a static table, even though its first
        # binding is a literal. Resolving single assignments must not resolve this one.
        'loop-only',
        'def test_a(client):\n'
        '    rows = [1]\n'
        '    rows = client.get("/x").json()\n'
        '    for r in rows:\n'
        '        assert r["ok"] == 1\n',
    ),
    ('no-assertion', 'def test_a():\n    compute_the_thing()\n'),
    ('unfalsifiable', 'def test_a():\n    assert 1 == 1\n'),
    ('unfalsifiable', 'def test_a():\n    assert True\n'),
    (
        'weak-only',
        'def test_a(page):\n'
        '    still_on_login = "/login" in page.url or page.locator("#email").is_visible()\n'
        '    assert still_on_login, "should not log in"\n',
    ),
    ('weak-only', 'def test_a(svc):\n    assert svc.result() is not None\n'),
    (
        'mock-only',
        'def test_a(mock_cleanup):\n'
        '    on_pipeline_error("u", "t")\n'
        '    mock_cleanup.assert_called_once_with("u")\n',
    ),
    (
        'mock-only',
        'def test_a(mock_update):\n'
        '    on_pipeline_error("u", "t")\n'
        '    assert mock_update.call_args[0][1] == 42\n',
    ),
    (
        'failure-masking',
        'def test_a():\n'
        '    try:\n'
        '        import fancy\n'
        '        assert fancy.version == 2\n'
        '    except ImportError:\n'
        '        pytest.skip("missing dep")\n',
    ),
    (
        'error-swallowed',
        'def test_a(svc):\n'
        '    try:\n'
        '        assert svc.masked() == "[PHONE]"\n'
        '    except Exception:\n'
        '        pass\n',
    ),
    (
        'error-swallowed',
        'def test_a(svc):\n'
        '    try:\n'
        '        assert svc.masked() == "[PHONE]"\n'
        '    except Exception as e:\n'
        '        logger.warning(e)\n',
    ),
    (
        # Six patch.object calls used to score 12 and three used to score 6; both read as
        # "6 references". Counting calls makes the threshold mean what it says.
        'mock-heavy',
        'def test_a(monkeypatch):\n'
        '    with patch.object(A, "x"), patch.object(B, "y"), patch("m.c"):\n'
        '        monkeypatch.setattr(D, "e", 1)\n'
        '        monkeypatch.setenv("F", "1")\n'
        '        monkeypatch.setitem(G, "h", 2)\n'
        '        assert run() == 3\n',
    ),
    ('fixture-named-test', '@pytest.fixture\ndef test_thing():\n    return 1\n'),
    (
        # The #400 near-miss, relabelled the way the convention forbids: an in-memory
        # stand-in wearing an id that claims the engine.
        'external-service-mock',
        'def test_delete_by_query_against_real_opensearch(fake_opensearch):\n'
        '    index_chunks(file_id=1, chunks=[])\n'
        '    assert fake_opensearch.deleted == [{"range": {"chunk_index": {"gte": 3}}}]\n',
    ),
    (
        # A marker is the strongest claim there is: `-m integration` SELECTS this test.
        'external-service-mock',
        '@pytest.mark.integration\n'
        'def test_stale_tail_is_pruned(monkeypatch):\n'
        '    monkeypatch.setattr("app.services.search.opensearch_client", _Fake())\n'
        '    assert prune(1) == 3\n',
    ),
    (
        # ...and it reaches every test in the module through `pytestmark`.
        'external-service-mock',
        'pytestmark = pytest.mark.integration\n'
        'def test_upload_writes_the_object():\n'
        '    with patch("boto3.client") as client:\n'
        '        store("k", b"v")\n'
        '    assert client.called\n',
    ),
    (
        # `skipif(SKIP_OPENSEARCH, ...)` declares "only runs against the real engine".
        'external-service-mock',
        '@pytest.mark.skipif(SKIP_OPENSEARCH, reason="needs the engine")\n'
        'def test_hybrid_search_ranks_by_rrf(monkeypatch):\n'
        '    monkeypatch.setattr("app.services.search.opensearch_service.client", _Fake())\n'
        '    assert rank(["a"]) == ["a"]\n',
    ),
    (
        # The bench bug: a readiness wait pinned to the DEV stack's container name, so it
        # reported healthy whenever dev was up and the stack under test never mattered.
        'readiness-probe-target',
        'def wait_for_bench_backend_health(timeout=180):\n'
        '    out = subprocess.check_output(["docker", "inspect", "opentranscribe-backend"])\n'
        '    return b"healthy" in out\n',
    ),
    (
        # Same defect through a module constant: resolving single assignments is what makes
        # `f"{BASE_URL}/health"` as hardcoded as the literal it came from.
        'readiness-probe-target',
        'BASE_URL = "http://localhost:5174/api"\n'
        'def test_stack_is_up():\n'
        '    try:\n'
        '        r = requests.get(f"{BASE_URL}/health", timeout=5)\n'
        '    except requests.ConnectionError:\n'
        '        pytest.skip("stack not running")\n'
        '    assert r.status_code == 200\n',
    ),
    (
        # The live shape, ruff-suppression and all: the DB error the commit is supposed to
        # raise is indistinguishable from one raised by the two `add()` calls above it.
        'broad-raises',
        'def test_a(db_session):\n'
        '    db_session.add(link)\n'
        '    with pytest.raises(Exception):  # B017 suppressed in the real file\n'
        '        db_session.commit()\n',
    ),
    (
        # `as exc` plus an assertion on `exc.value` does NOT narrow it: any exception
        # carrying `status_code == 403` satisfies both, including one from argument setup.
        'broad-raises',
        'def test_a(world):\n'
        '    with pytest.raises(Exception) as exc:\n'
        '        get_file_by_uuid_with_permission(world.db, "u", 1, organization_id=2)\n'
        '    assert getattr(exc.value, "status_code", None) == 403\n',
    ),
    (
        # The live instance: nothing was prepared, so nothing is asserted and nothing is
        # reported. `conditional-skip` cannot see this — there is no `pytest.skip`.
        'bare-return',
        'def test_a(files):\n'
        '    prepared = [f for f in files if prepare(f)]\n'
        '    if not prepared:\n'
        '        print("No files were prepared successfully.")\n'
        '        return\n'
        '    assert process(prepared) == len(prepared)\n',
    ),
    (
        'self-comparison',
        'def test_cache_key_is_stable_within_one_corpus_version():\n'
        '    assert _key_with_corpus("7") == _key_with_corpus("7")\n',
    ),
    (
        # `time.sleep(0.5)  # let NVML settle` then measure. If the driver has not settled
        # the assertion runs anyway, and on a fast machine it passes for the wrong reason.
        'sleep-sync',
        'def test_a():\n'
        '    torch.cuda.empty_cache()\n'
        '    time.sleep(0.5)\n'
        '    assert _nvml_used_mb() - baseline <= RESIDUE_GATE_MB\n',
    ),
    (
        # A zero-duration yield must not launder a REAL wait sitting beside it. Without
        # this the `sleep(0)` exemption could be widened to "any sleep in an async test".
        'sleep-sync',
        'async def test_a():\n'
        '    await asyncio.sleep(0)\n'
        '    await asyncio.sleep(0.5)\n'
        '    assert _delivered() == 3\n',
    ),
    (
        # Inside a poll loop, but on the branch that LEAVES it: the poll already succeeded
        # and the 5 s is a bare bet on start-up. A whole-loop exemption would clear this.
        'sleep-sync',
        'def test_a():\n'
        '    for _ in range(60):\n'
        '        if _is_port_open("localhost", 8180):\n'
        '            time.sleep(5)\n'
        '            break\n'
        '        time.sleep(2)\n'
        '    assert _is_port_open("localhost", 8180)\n',
    ),
    ('skipped-test', '@pytest.mark.skip(reason="flaky")\ndef test_a():\n    assert run() == 1\n'),
    (
        'skipped-test',
        '@pytest.mark.xfail(reason="not implemented")\ndef test_a():\n    assert run() == 1\n',
    ),
)

#: Sources that must produce NO finding — the false-positive half of the calibration.
SELFTEST_CLEAN: tuple[str, ...] = (
    # `asyncio.sleep(0)` yields to the loop once and bounds NOTHING, so it cannot be the
    # "hope it is ready by now" bet sleep-sync exists to catch. It is also unavoidable:
    # `loop.call_soon_threadsafe` schedules a callback rather than running it, so a test
    # asserting on the result must let the loop turn. Flagging it would put a permanent
    # allowlist entry on every async test in the repo.
    'async def test_a():\n'
    '    recorder.record(event)\n'
    '    await asyncio.sleep(0)\n'
    '    assert _drain(queue) == [event]\n',
    # Redirecting a REAL client at a throwaway index is the correct shape for an
    # integration test: `settings.OPENSEARCH_CHUNKS_INDEX` is the index NAME, not the
    # client, and every assertion still executes on the real engine. The detector fired on
    # all 8 tests in tests/integration/test_rename_propagation_chunks.py for this
    # (reported by #403). A detector that fires on correct code teaches people to
    # allowlist, and an allowlist habit is how the 41-finding backlog formed.
    '@pytest.mark.integration\n'
    'def test_rename_propagates_to_chunks(chunk_index, monkeypatch):\n'
    '    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", chunk_index)\n'
    '    assert propagate(1) == 3\n',
    # The dotted-string form of the same thing.
    '@pytest.mark.integration\n'
    'def test_reindex_uses_the_scratch_index():\n'
    '    with patch("app.core.config.settings.OPENSEARCH_CHUNKS_INDEX", "scratch"):\n'
    '        assert reindex(1) == 3\n',
    # An exact status assertion with the real check unguarded: the shape the fixes produce.
    'def test_a(client):\n'
    '    r = client.get("/x")\n'
    '    assert r.status_code == 200, r.text\n'
    '    assert r.json()["my_permission"] == "owner"\n',
    # A non-emptiness assertion outside the loop makes the loop body reachable-or-fail.
    'def test_a(detector):\n'
    '    spans = detector.detect(text)\n'
    '    assert spans, "detector found nothing"\n'
    '    for s in spans:\n'
    '        assert 0 <= s.start < s.end\n',
    # A literal iterable cannot be empty.
    'def test_a():\n    for n in (1, 2, 3):\n        assert n > 0\n',
    # `range(3)` cannot be empty either.
    'def test_a():\n    for n in range(3):\n        assert n >= 0\n',
    # A local bound ONCE to a literal table is as static as the literal (22 table-driven
    # tests were false positives before single-assignment resolution).
    'def test_a(client):\n'
    '    endpoints = ["/api/a", "/api/b"]\n'
    '    for e in endpoints:\n'
    '        assert client.get(e).status_code == 401\n',
    # Same for a module-level constant, and for `.items()` over a literal mapping.
    'SCHEMAS = {"a": 1, "b": 2}\n'
    'def test_a():\n'
    '    for name, value in SCHEMAS.items():\n'
    '        assert value > 0, name\n',
    # pytest.raises IS an assertion (no-assertion must not fire).
    'def test_a():\n    with pytest.raises(ValueError):\n        parse("nope")\n',
    # Playwright's expect() is the only assertion in most E2E tests.
    'def test_a(page):\n    expect(page.locator("#x")).to_be_visible()\n',
    # A mock call assertion ALONGSIDE a real one is not mock-only.
    'def test_a(mock_send):\n'
    '    result = run()\n'
    '    mock_send.assert_called_once_with(7)\n'
    '    assert result == {"batch_id": "b1"}\n',
    # A predicate call is real evidence, not bare truthy.
    'def test_a():\n    assert is_valid_uuid(value)\n',
    # `assert not <violations>` is the AST-guard idiom and fails on any violation.
    'def test_a():\n'
    '    offenders = scan_the_tree()\n'
    '    assert not offenders, f"unmarked DDL in {offenders}"\n',
    # A guarded assertion plus an unguarded one is not conditional-only.
    'def test_a(flag):\n    assert base() == 1\n    if flag:\n        assert flag.value == 2\n',
    # An except handler that asserts on the exception is neither masking nor swallowing.
    'def test_a():\n'
    '    try:\n'
    '        parse("nope")\n'
    '    except ValueError as e:\n'
    '        assert "nope" in str(e)\n',
    # Two patches is normal test setup, not scaffolding.
    'def test_a():\n    with patch("m.a"), patch.object(B, "c"):\n        assert run() == 1\n',
    # A real fixture that is not named test_*.
    '@pytest.fixture\ndef thing():\n    return 1\n',
    # `if r.status_code != 200:` is control flow, not a claim — negated-status must not fire.
    'def test_a(client):\n'
    '    r = client.get("/x")\n'
    '    if r.status_code != 200:\n'
    '        pytest.fail(r.text)\n'
    '    assert r.json()["ok"] == 1\n',
    # A stand-in under an id that claims nothing is the CORRECT half of the convention —
    # this is the #400 suite as actually written, and it must stay clean.
    'def test_stale_tail_is_dropped(fake_opensearch):\n'
    '    index_chunks(1, [])\n'
    '    assert fake_opensearch.deleted == ["chunk_index >= 3"]\n',
    # "real" qualifying something that is not the service. The first false positive this
    # detector produced: "a real session", with Redis faked, and Redis nowhere in the name.
    'def test_a_real_session_still_authorizes_enrollment(fake_redis):\n'
    '    assert authorize(session="s1") == "ok"\n',
    # The service NAMING itself is not a claim — it names the subject. Twenty tests of this
    # exact shape fired before that tier was removed.
    'def test_blacklist_token_redis_unavailable_fails_secure(mock_redis_unavailable):\n'
    '    assert blacklist("t") is False\n',
    # One derived argument means the probe follows the stack under test: a subprocess on an
    # allocated port is legitimately on 127.0.0.1.
    'def _wait_until_serving(port):\n    return _reachable("127.0.0.1", port)\n',
    # The conftest shape after 7c989d51: probe the endpoint the suite will actually use.
    'PORT = int(os.environ.get("MINIO_PORT", "5178"))\n'
    'def _service_reachable():\n'
    '    return socket.create_connection(("localhost", PORT), timeout=1)\n',
    # A relative path against a fixture-provided client carries no endpoint at all, so a
    # readiness TEST is not a readiness probe with a hardcoded target.
    'def test_readiness_endpoint_reports_degraded(client):\n'
    '    r = client.get("/api/readiness")\n'
    '    assert r.json()["status"] == "degraded"\n',
    # A named exception is the whole point of pytest.raises — broad-raises must not fire.
    'def test_a():\n    with pytest.raises(ValueError):\n        parse("nope")\n',
    # `match=` pins WHICH failure, which is the narrowing the broad form lacks.
    'def test_a():\n'
    '    with pytest.raises(Exception, match="quarantined"):\n'
    '        read(file_id=1)\n',
    # A return AFTER an assertion: the rest of the test is genuinely conditional. Four of the
    # five bare returns in this tree are this shape (e2e/test_chat.py, e2e/test_mfa.py,
    # unit/test_auth_config_has_readers.py) and firing on them makes the detector noise.
    'def test_a(client):\n'
    '    assert client.get("/x").status_code == 200\n'
    '    if not llm_configured():\n'
    '        return\n'
    '    assert client.get("/y").json()["suggestions"]\n',
    # `pytest.skip(...)` then `return  # unreachable, satisfies mypy` — the skip IS the report,
    # and the return is bookkeeping (e2e/test_gallery_actions.py::test_retry_failed_api).
    'def test_a(client):\n'
    '    error_file = _get_error_file(client)\n'
    '    if error_file is None:\n'
    '        pytest.skip("No error files to test retry")\n'
    '        return\n'
    '    assert retry(error_file).status_code == 200\n',
    # A `return` inside a helper CLOSURE is that helper's control flow, not the test bailing.
    'def test_a(monkeypatch):\n'
    '    def fake_fetch(url):\n'
    '        return\n'
    '    monkeypatch.setattr(svc, "fetch", fake_fetch)\n'
    '    assert svc.run() == 1\n',
    # A self-comparison BESIDE real assertions pins determinism of a value the others already
    # constrain. unit/test_chat_retrieval.py::test_scope_hash_is_order_independent, verbatim.
    'def test_scope_hash_is_order_independent():\n'
    '    assert scope_hash(["b", "a"]) == scope_hash(["a", "b"])\n'
    '    assert scope_hash(None) == scope_hash(None)\n'
    '    assert scope_hash(None) != scope_hash(["a"])\n',
    # A poll loop re-checks and exits early, so the sleep bounds LATENCY, not correctness.
    # Eleven of this tree's sixteen blocking sleeps are this shape.
    'def _wait_for(predicate, timeout=10.0, interval=0.05):\n'
    '    deadline = time.monotonic() + timeout\n'
    '    while time.monotonic() < deadline:\n'
    '        if predicate():\n'
    '            return True\n'
    '        time.sleep(interval)\n'
    '    return False\n',
    # Retry-with-backoff: the same shape written as a `for` with a `break` nested in an `if`.
    'def test_a():\n'
    '    for attempt in range(4):\n'
    '        resp = requests.post(URL, timeout=10)\n'
    '        if resp.status_code == 200:\n'
    '            break\n'
    '        time.sleep(5 * (attempt + 1))\n'
    '    assert resp.status_code == 200\n',
    # `skipif` is how this repo gates the RUN_* suites and is visible in the run.
    '@pytest.mark.skipif(not RUN_PKI_TESTS, reason="RUN_PKI_TESTS unset")\n'
    'def test_a():\n'
    '    assert pki_login() == 200\n',
    # `xfail(strict=True)` FAILS when the test starts passing, so it cannot outlive its
    # subject. api/endpoints/test_comments.py carries two of these.
    '@pytest.mark.xfail(strict=True, reason="update_comment takes no Request")\n'
    'def test_a():\n'
    '    assert update_comment(1) == 403\n',
)

#: The #400 suite's ACTUAL fixture, reduced. It is the regression guard for the defect that
#: made ``external-service-mock`` useless in practice: the stand-in is installed by a fixture
#: called ``fake_index``, which never names OpenSearch, so neither a patch-only scan of the
#: test body nor a look at the parameter name saw anything. Only resolving the fixture's own
#: body does. Before `services_by_fixture` existed, the real file stayed clean even when
#: deliberately mislabelled — the detector fired on its own synthetic fixture and nothing else.
_FIXTURE_RESOLUTION_SOURCE = (
    '@pytest.fixture\n'
    'def fake_index(monkeypatch):\n'
    '    client = _FakeIndex()\n'
    "    monkeypatch.setattr(svc, 'opensearch_client', client)\n"
    '    return client\n'
    '\n'
    'def test_shrinking_rechunk_leaves_no_stale_tail(fake_index):\n'
    '    assert index_chunks(1, []) == 3\n'
)

#: Must-fire cases whose verdict depends on the MODULE PATH, which the fixtures above cannot
#: express (they are all scanned as ``fixture.py``). ``(category, path, source)``.
SELFTEST_PATH_CASES: tuple[tuple[str, str, str], ...] = (
    (
        # `tests/integration/` is a positional claim: the directory says real stack.
        'external-service-mock',
        'integration/test_chunk_reindex.py',
        'def test_stale_tail_is_pruned(fake_opensearch):\n'
        '    index_chunks(1, [])\n'
        '    assert fake_opensearch.deleted == ["chunk_index >= 3"]\n',
    ),
    (
        # The real shape, mislabelled by LOCATION. Verified against the actual #400 file.
        'external-service-mock',
        'integration/test_search_chunk_pruning.py',
        _FIXTURE_RESOLUTION_SOURCE,
    ),
    (
        # ...and the same body mislabelled by MARKER, which is the stronger claim: a
        # `-m integration` run selects it as real-engine coverage.
        'external-service-mock',
        'unit/test_search_chunk_pruning.py',
        'pytestmark = pytest.mark.integration\n' + _FIXTURE_RESOLUTION_SOURCE,
    ),
)

#: ``(path, source)`` pairs that must stay clean. The first is the second half of the
#: convention made executable: the SAME test body, moved to a directory that declares what it
#: is, is correctly labelled — which is the whole point of "different ids".
SELFTEST_PATH_CLEAN: tuple[tuple[str, str], ...] = (
    (
        'unit/test_chunk_reindex.py',
        'def test_stale_tail_is_pruned(fake_opensearch):\n'
        '    index_chunks(1, [])\n'
        '    assert fake_opensearch.deleted == ["chunk_index >= 3"]\n',
    ),
    # The #400 suite exactly as it really ships: a stand-in, honestly located, claiming
    # nothing. Fixture resolution must not turn "uses a fake" into a finding on its own —
    # 388 tests in this tree substitute a service and 264 of them are only visible through
    # this resolution, so a claim-side slip would report all of them.
    ('unit/test_search_chunk_pruning.py', _FIXTURE_RESOLUTION_SOURCE),
)

#: ``(category, path, source)`` where the category must fire **exactly once**.
#:
#: The fires/clean pair cannot express "reported twice". One defect reported under two names
#: costs two allowlist entries and reads as two problems, and `readiness-probe-target` did
#: exactly that: `scan_source` scans every function reached by ``ast.walk`` — methods and
#: nested functions included — and then scans module scope separately, so a probe inside a test
#: METHOD was reported once as the method and again as ``<module>``. Both a must-fire and a
#: must-stay-clean case pass happily while that is broken.
SELFTEST_ONCE: tuple[tuple[str, str, str], ...] = (
    (
        'readiness-probe-target',
        'test_stack.py',
        'class TestStack:\n'
        '    def test_the_api_is_up(self):\n'
        '        try:\n'
        '            r = requests.get("http://localhost:5174/api/health", timeout=1)\n'
        '        except requests.ConnectionError:\n'
        '            pytest.skip("dev stack not running")\n'
        '        assert r.json()["status"] == "ok"\n',
    ),
)
