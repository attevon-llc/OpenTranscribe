"""In-memory fixtures the self-test feeds the detectors: must-fire, must-stay-clean, exactly-once."""

from __future__ import annotations

SELFTEST_CASES: tuple[tuple[str, str], ...] = (
    (
        'session-subprocess',
        'def task(x):\n'
        '    with session_scope() as db:\n'
        '        row = db.query(M).first()\n'
        '        subprocess.run(["ffmpeg", "-i", row.path], check=False)\n',
    ),
    (
        'session-object-storage',
        'def task(x):\n'
        '    with session_scope() as db:\n'
        '        row = db.query(M).first()\n'
        '        minio_service.download_file(object_name=row.path, file_path="/tmp/a")\n',
    ),
    (
        'session-search-engine',
        'def task(ids):\n'
        '    with session_scope() as db:\n'
        '        for i in ids:\n'
        '            svc.reindex_transcript(file_uuid=i)\n',
    ),
    (
        'session-http',
        'def task():\n    with session_scope() as db:\n        httpx.post(url, json={})\n',
    ),
    # A session opened through an injected FACTORY is still a session. The phased chat
    # turn (e486f948) passes `session_factory` instead of a `Session` so each statement
    # group gets its own short transaction; before this case the detector could not fire
    # inside such a block at all.
    (
        'session-search-engine',
        'def counted(session_factory, uuids):\n'
        '    with session_factory() as db:\n'
        '        rows = db.query(M).all()\n'
        '        svc.reindex_transcript(file_uuid=rows[0].uuid)\n',
    ),
    # ...and through the @contextmanager wrapper around it, which is where a caller's slow
    # work would actually land: the wrapper's own body is a bare yield, so matching only
    # `session_factory` would leave every real call site invisible.
    (
        'session-llm',
        'def counted(session_factory, question):\n'
        '    with _short_session(session_factory) as db:\n'
        '        mf = db.query(M).first()\n'
        '        svc.generate_summary(transcript=mf.text)\n',
    ),
    (
        'session-llm',
        'def task(uuid):\n'
        '    with session_scope() as db:\n'
        '        mf = db.query(M).first()\n'
        '        svc.generate_summary(transcript=mf.text)\n',
    ),
    (
        'session-model',
        'def task():\n'
        '    with session_scope() as db:\n'
        '        service = get_cached_embedding_service()\n',
    ),
    (
        'session-smtp',
        'def task(sid):\n'
        '    with session_scope() as db:\n'
        '        for cfg in db.query(C).all():\n'
        '            send_email(cfg, ["a@b"], "s", "<p/>")\n',
    ),
    (
        'session-thread-pool',
        'def task():\n'
        '    with session_scope() as db:\n'
        '        with ThreadPoolExecutor(max_workers=8) as pool:\n'
        '            pool.submit(work)\n',
    ),
    # #403: `legs.run_legs` blocks on every leg's future exactly like a
    # bare `with ThreadPoolExecutor()`, but the pool itself lives OUTSIDE the
    # call site (`legs.get_executor`'s lazy singleton) — nothing about this
    # call looks like a thread pool unless the name is listed.
    (
        'session-thread-pool',
        'def prep(assistant_message_uuid):\n'
        '    with session_scope() as db:\n'
        '        built = [Leg(kind="main", name="main", run=lambda: retrieve_chunks("q"))]\n'
        '        outcome = legs.run_legs(built, max_workers=4)\n',
    ),
    (
        # THE interprocedural case: this is `_perform_scan(db, source, ...)`, the exact
        # shape whose slow work sat one frame below the caller's `session_scope`.
        'session-param-slow-work',
        'def _perform_scan(db, source, summary):\n'
        '    with create_client(source) as client:\n'
        '        files = client.list_files()\n'
        '        for f in files:\n'
        '            client.download_file(f.path, "/tmp/x")\n',
    ),
    (
        # ...and the annotated variant, which must not depend on the parameter's name.
        'session-param-slow-work',
        'def render(session: Session, file_id: int) -> None:\n'
        '    subprocess.run(["ffmpeg"], check=False)\n',
    ),
    (
        # An async task holds the transaction exactly as long.
        'session-object-storage',
        'async def task(x):\n'
        '    async with session_scope() as db:\n'
        '        await minio_client.put_object(b, k, v)\n',
    ),
    (
        # Nested one frame down inside the with-body still counts: `if`/`for`/`try` do not
        # end the transaction.
        'session-subprocess',
        'def task(items):\n'
        '    with session_scope() as db:\n'
        '        for i in items:\n'
        '            try:\n'
        '                if i.ready:\n'
        '                    subprocess.run(["ffmpeg"], check=False)\n'
        '            except OSError:\n'
        '                pass\n',
    ),
    (
        # A CONDITIONAL close is not a release. On the branch that skips it the handler
        # holds the request transaction across the round trip, which is the actual defect
        # — so top-level-only detection is deliberate, not a simplification.
        'session-param-slow-work',
        'def handler(db, uuid):\n'
        '    row = db.query(M).first()\n'
        '    if row.needs_close:\n'
        '        db.close()\n'
        '    opensearch_client.search(index="i", body={})\n',
    ),
    (
        # Closed, then RE-ARMED by a later query. Everything after the re-arm is held
        # again, so the read -> close -> slow -> reopen -> write shape must still be
        # checked past the reopen.
        'session-param-slow-work',
        'def handler(db, uuid):\n'
        '    row = db.query(M).first()\n'
        '    db.close()\n'
        '    again = db.query(M).first()\n'
        '    opensearch_client.search(index="i", body={})\n',
    ),
)

#: Sources that MUST stay silent. These are the real three-phase fix shapes — if the
#: detector flags them, the gate punishes the correct code and will be turned off.
SELFTEST_CLEAN: tuple[str, ...] = (
    # THE ENDPOINT CURE. A `Depends(get_db)` session lives for the request and cannot be
    # wrapped in a `with`, so the fix is read -> close -> slow work. Flagging this makes the
    # gate punish the cure; a real fix was renamed purely to dodge a path substring before
    # the release rule existed.
    'def handler(db, uuid):\n'
    '    row = db.query(M).filter(M.uuid == uuid).first()\n'
    '    name = str(row.filename)\n'
    '    db.close()\n'
    '    opensearch_client.search(index="i", body={"q": name})\n'
    '    minio_service.download_file(object_name=name, file_path="/tmp/a")\n',
    # The factory shape done RIGHT — the must-stay-clean twin of the two cases above.
    # Read inside the short session, close it, then do the slow work. Without this, adding
    # the factory names could only ever make the gate noisier, and nothing would prove the
    # detector still distinguishes the fix from the defect.
    'def counted(session_factory, uuids):\n'
    '    with _short_session(session_factory) as db:\n'
    '        rows = db.query(M).all()\n'
    '        names = [str(r.filename) for r in rows]\n'
    '    opensearch_client.search(index="i", body={"q": names})\n',
    # The canonical fix: read -> slow work with no session -> write.
    'def task(uuid):\n'
    '    with session_scope() as db:\n'
    '        row = db.query(M).filter(M.uuid == uuid).first()\n'
    '        path = str(row.storage_path)\n'
    '    minio_service.download_file(object_name=path, file_path="/tmp/a")\n'
    '    subprocess.run(["ffmpeg", "-i", "/tmp/a"], check=False)\n'
    '    with session_scope() as db:\n'
    '        db.query(M).filter(M.uuid == uuid).update({"status": "done"})\n',
    # A session body doing only DB work is the POINT of a session, not a finding.
    'def task(uuid):\n'
    '    with session_scope() as db:\n'
    '        rows = db.query(M).filter(M.uuid == uuid).all()\n'
    '        for r in rows:\n'
    '            r.status = "done"\n'
    '        db.flush()\n',
    # A callee that takes plain data and opens its OWN short session is the fix pattern
    # the interprocedural rule is asking for — it must not fire on the cure.
    'def _load(uuid):\n'
    '    with session_scope() as db:\n'
    '        return str(db.query(M).filter(M.uuid == uuid).first().storage_path)\n'
    '\n'
    'def _transfer(storage_path):\n'
    '    minio_service.download_file(object_name=storage_path, file_path="/tmp/a")\n',
    # A parameter annotated as something else is not a session, whatever it is called.
    'def render(db: str, file_id: int) -> None:\n    subprocess.run(["ffmpeg", db], check=False)\n',
    # Slow work AFTER the session block closes, in the same function.
    'def task(uuid):\n'
    '    with session_scope() as db:\n'
    '        name = str(db.query(M).first().filename)\n'
    '    with ThreadPoolExecutor(max_workers=4) as pool:\n'
    '        pool.submit(work, name)\n',
    # A pure-CPU helper with no session and no slow call.
    'def _merge(segments):\n    return sorted(segments, key=lambda s: s["start"])\n',
    # #403's real fix shape: `run_legs` called with NO session open at
    # all — each leg opens its own SHORT session internally (a different
    # function's body, invisible to a call-site scan, which is the point).
    'def prep(assistant_message_uuid):\n'
    '    built = [Leg(kind="main", name="main", run=lambda: retrieve_chunks("q"))]\n'
    '    outcome = legs.run_legs(built, max_workers=4)\n'
    '    return outcome\n',
    # Benign methods on a variable whose NAME merely contains a rule keyword. Reading
    # `opensearch_result.get(...)` is a dict lookup and `llm_service.close()` releases a
    # client; flagging them five times in one endpoint is how a gate loses its audience.
    'def get_file_summary(db, file_id):\n'
    '    with session_scope() as db:\n'
    '        opensearch_result = cached\n'
    '        bluf = opensearch_result.get("bluf")\n'
    '        llm_service.close()\n'
    '        return bluf\n',
)

#: ``(category, source)`` pairs that must fire EXACTLY once — a rule that double-counts one
#: call inflates every number the gate reports.
SELFTEST_ONCE: tuple[tuple[str, str], ...] = (
    (
        'session-object-storage',
        'def task(x):\n'
        '    with session_scope() as db:\n'
        '        minio_service.upload_file(file_path="/tmp/a", object_name="k")\n',
    ),
)
