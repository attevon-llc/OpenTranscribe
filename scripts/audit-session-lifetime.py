#!/usr/bin/env python3
"""Find DB sessions held open across slow non-DB work.

This is the single most repeated defect in this codebase, and it has wedged the live
database twice in one day on two different workers. A task that wraps its body in one
``with session_scope() as db:`` and then runs ffmpeg, a MinIO transfer, an OpenSearch
round trip, an LLM completion, an SMTP send or a model load **inside** it holds a
Postgres transaction for the whole duration. A plain SELECT takes ACCESS SHARE for the
life of its transaction, so such a hold:

1. **queues every ``ALTER TABLE``** — i.e. it hangs an Alembic upgrade mid-release, and
   dev runs migrations automatically on backend startup;
2. **pins the VACUUM horizon** on ``transcript_segment``, the largest table in the product;
3. **consumes a pool connection** for as long as it lives.

Measured: a CPU worker ``idle in transaction`` for 48 minutes (a 3-hour task killed by the
hard time limit) and an NLP worker for 1 h 26 m. Both were found only because DDL migration
tests started failing with ``psycopg2.errors.LockNotAvailable`` and the suite went
138.9 s → 224.8 s.

The rule and the three-phase fix pattern live in ``backend/app/tasks/CLAUDE.md``;
``backend/app/tasks/speaker_attribute_task.py`` is the worked example.

Detectors
    session-subprocess
        ``subprocess.run``/``Popen``, or one of this repo's ffmpeg wrappers, inside a session
        body. An ffmpeg transcode is minutes, and ``timeout=300`` bounds the process, not
        the transaction.
    session-object-storage
        A MinIO / S3 / boto transfer inside a session body. Uploads and downloads here are
        gigabytes over the network.
    session-search-engine
        An OpenSearch round trip inside a session body. Individually fast, but these appear
        in per-item loops: 50 re-index calls or 2N profile writes in ONE transaction.
    session-http
        ``requests`` / ``httpx`` / ``aiohttp`` / ``urllib`` inside a session body. An outbound
        request is bounded only by its own timeout — and often has none.
    session-llm
        An LLM completion inside a session body. Multi-minute over a whole transcript, and a
        provider stall is bounded only by the HTTP timeout. This is what held the NLP worker's
        transaction for 1 h 26 m.
    session-model
        A model load or an inference pass inside a session body. A cold wav2vec2/Whisper load
        is 40-60 s before any work starts.
    session-smtp
        An SMTP / Graph mail send inside a session body — 30 s of timeout **per recipient
        config**, spent holding the cluster-wide vacuum horizon for nothing.
    session-thread-pool
        A ``ThreadPoolExecutor``/``ProcessPoolExecutor`` block inside a session body.
        ``__exit__`` calls ``shutdown(wait=True)`` with **no timeout**, so a per-future
        ``result(timeout=30)`` is decorative: one wedged child holds the block, and the
        transaction, indefinitely.
    session-param-slow-work
        **The interprocedural rule.** A function that both ACCEPTS a ``Session`` and performs
        slow work. The caller's ``with session_scope()`` is then one frame up, where no
        body-scan can see it — which is exactly how ``watch_source.scan_single`` hid: its
        session wrapped ``_perform_scan(db, ...)``, and the remote listing, the per-file
        download and the MinIO upload were all a frame further down. Passing ``db`` into slow
        work is also how the idiom SPREADS; the fix is to take the data, or open a short
        session inside.

Usage::

    scripts/audit-session-lifetime.py backend/app
    scripts/audit-session-lifetime.py backend/app --json
    scripts/audit-session-lifetime.py backend/app --category session-llm
    scripts/audit-session-lifetime.py --selftest     # audit the auditor (no tree needed)

Exits 1 when any finding is not in the allowlist, so this can gate a commit. The allowlist is
``scripts/session-lifetime-allowlist.txt``: one ``<file>::<scope>::<category>  # reason`` per
line, all three segments required and the reason mandatory. The category is part of the key on
purpose — an entry keyed by function alone would exempt it from every detector at once.

**The allowlist is COUNT-AWARE and can only SHRINK.** One line buys one finding, duplicate
keys encode a count, and an entry with no finding left to cover FAILS the run. Fix a call,
delete its line. A reason starting ``BACKLOG`` marks deferred work and is counted and printed
separately, so a green gate is never read as a clean tree.

**Run ``--selftest`` after touching any detector.** A detector that matches nothing reports
zero findings, which is indistinguishable from a clean codebase — this repo's most repeated
failure mode, and the reason four separate gates were found silently not working. Every
detector has a must-fire case AND the clean cases (the real three-phase fix shapes) must stay
silent. ``backend/tests/unit/test_session_lifetime_audit.py`` runs the same cases under
pytest, so the suite fails if a detector goes blind even when nobody remembers the flag.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_ALLOWLIST_PATH = Path(__file__).resolve().parent / 'session-lifetime-allowlist.txt'

#: Reason prefix marking an entry as DEFERRED WORK rather than an accepted pattern.
_BACKLOG_PREFIX = 'BACKLOG'

CATEGORIES = (
    'session-subprocess',
    'session-object-storage',
    'session-search-engine',
    'session-http',
    'session-llm',
    'session-model',
    'session-smtp',
    'session-thread-pool',
    'session-param-slow-work',
)


@dataclass(frozen=True)
class Finding:
    """One slow call reachable with a DB transaction open."""

    category: str
    path: str
    line: int
    scope: str
    detail: str

    @property
    def key(self) -> str:
        """Allowlist key. Includes the category so one exemption cannot cover all nine."""
        return f'{self.path}::{self.scope}::{self.category}'


# --------------------------------------------------------------------------- slow-call rules
#
# Each rule is (category, exact call names, dotted-path substrings). A call fires the rule
# when its FINAL attribute is in the name set, or when its dotted path contains one of the
# substrings. Both are needed: `subprocess.run` is only recognisable by its path, while
# `client.download_file` is only recognisable by its method name.


@dataclass(frozen=True)
class Rule:
    category: str
    names: frozenset[str]
    paths: tuple[str, ...]


RULES: tuple[Rule, ...] = (
    Rule(
        'session-subprocess',
        frozenset(
            {
                'run_ffmpeg',
                '_run_ffmpeg',
                '_probe_audio_codec',
                'stitch_files',
                'extract_audio_from_video',
                'extract_audio_segment_np',
                'prepare_audio_for_transcription',
                'check_output',
                'check_call',
            }
        ),
        ('subprocess.run', 'subprocess.popen', 'subprocess.call', 'os.system', 'ffmpeg'),
    ),
    Rule(
        'session-object-storage',
        frozenset(
            {
                'upload_file',
                'upload_file_tuned',
                'upload_bytes',
                'download_file',
                'download_temp_audio',
                'presigned_get_object',
                'presigned_put_object',
                'get_presigned_url',
                'get_presigned_download_url',
                'fput_object',
                'fget_object',
                'put_object',
                'get_object',
                'remove_object',
                'delete_object',
                'copy_object',
                'delete_file_storage_artifacts',
            }
        ),
        ('minio', 'boto3', 's3_client.', 'storage_backend.'),
    ),
    Rule(
        'session-search-engine',
        frozenset(
            {
                # (``index_summary`` / ``delete_summary`` / ``get_max_version`` were
                # here for the retired ``transcript_summaries`` index, #67. Two of
                # those names no longer exist anywhere; ``delete_summary`` now names
                # a Postgres-only endpoint handler, so keeping it would make this
                # detector fire on a call that touches no search engine.)
                'reindex_transcript',
                'store_profile_embedding_v4',
                'bulk_add_speaker_embeddings_v4',
                'add_speaker_embedding',
                'get_speaker_document',
                'update_speaker_segment_count',
                'ensure_v4_index_exists',
                'delete_by_query',
                'msearch',
            }
        ),
        ('opensearch', 'os_client.'),
    ),
    Rule(
        'session-http',
        frozenset({'urlopen'}),
        (
            'requests.get',
            'requests.post',
            'requests.put',
            'requests.delete',
            'requests.request',
            'httpx.',
            'aiohttp.',
            'urllib.request',
        ),
    ),
    Rule(
        'session-llm',
        frozenset(
            {
                'generate_summary',
                'identify_speakers',
                'extract_topics',
                'chat_completion',
                'create_from_user_settings',
                'create_from_system_settings',
            }
        ),
        ('completions.create', '.chat.completions'),
    ),
    Rule(
        'session-model',
        frozenset(
            {
                'load_models',
                'load_model',
                'from_pretrained',
                'get_cached_embedding_service',
                'get_cached_attribute_service',
                '_run_inference',
                'extract_embeddings_for_segments',
                'extract_embedding_from_file',
                'transcribe_audio',
                'diarize_audio',
            }
        ),
        ('whisperx.', 'pyannote.', 'torch.load'),
    ),
    Rule(
        'session-smtp',
        frozenset({'send_email', 'sendmail', 'send_message'}),
        ('smtplib.',),
    ),
    Rule(
        'session-thread-pool',
        frozenset({'ThreadPoolExecutor', 'ProcessPoolExecutor', 'as_completed'}),
        ('concurrent.futures',),
    ),
)

#: Callables that OPEN a database session as a context manager.
#: ``session_factory`` and ``_short_session`` were added after the chat turn was phased
#: (e486f948): ``answer_aggregation`` takes a FACTORY rather than a ``Session``, precisely so
#: each statement group gets its own short transaction. Without both names the detector saw
#: those blocks as ordinary ``with`` statements and could not fire inside them at all —
#: reporting 0 findings, which is indistinguishable from a clean subsystem.
#:
#: BOTH are needed, and ``_short_session`` is the load-bearing one. ``session_factory``
#: only covers the direct ``with session_factory() as db:`` inside the helper, whose body is
#: a bare ``yield`` — nothing slow can live there. The CALLER's work runs during that yield,
#: in its own ``with _short_session(...)`` block, and that is the block a slow call would be
#: added to.
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


def _dotted(node: ast.AST) -> str:
    """Best-effort dotted path for a call target (``a.b.c`` from ``a.b.c(...)``)."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    elif isinstance(current, ast.Call):
        parts.append(_dotted(current.func))
    return '.'.join(reversed(parts))


def _last_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ''


def _classify(call: ast.Call) -> Rule | None:
    """Return the rule this call trips, or None."""
    name = _last_name(call.func)
    dotted = _dotted(call.func).lower()
    for rule in RULES:
        if name in rule.names:
            return rule
    if name in _BENIGN_LAST_NAMES:
        return None
    for rule in RULES:
        if any(fragment in dotted for fragment in rule.paths):
            return rule
    return None


def _opens_session(item: ast.withitem) -> bool:
    """True when a ``with`` item opens a DB session."""
    ctx = item.context_expr
    if isinstance(ctx, ast.Call):
        return _last_name(ctx.func) in _SESSION_OPENERS
    return _last_name(ctx) in _SESSION_OPENERS


#: Session methods that put the caller back INTO a transaction after a close.
_REARMS_TRANSACTION = frozenset(
    {'query', 'execute', 'add', 'delete', 'merge', 'refresh', 'flush', 'scalar', 'get'}
)


def _release_line(fn: ast.FunctionDef | ast.AsyncFunctionDef, param: str) -> int | None:
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


def _takes_session(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The name of a parameter that is somebody else's ``Session``, or None.

    Annotation first — that is unambiguous. The name fallback (``db``/``session``) exists
    because a third of this codebase's helpers are annotated only as ``db`` with no type;
    ``_perform_scan(db, source, ...)``, the function that hid the ``scan_single`` leak, is
    exactly that shape and an annotation-only rule would have missed it again.
    """
    args = fn.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        annotation = arg.annotation
        if annotation is not None and _last_name(annotation) in _SESSION_ANNOTATIONS:
            return arg.arg
        if annotation is not None:
            # A parameter annotated as something else is NOT a session, whatever it is
            # called (`db: str` in a config helper).
            continue
        if arg.arg in ('db', 'db_session'):
            return arg.arg
    return None


def _enclosing_scopes(tree: ast.AST) -> dict[ast.AST, str]:
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


def _nearest_scope(
    node: ast.AST, owner: dict[ast.AST, str], parents: dict[ast.AST, ast.AST]
) -> str:
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
    owner = _enclosing_scopes(tree)

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
                detail=f'{_dotted(call.func) or _last_name(call.func)}() {why}',
            )
        )

    # --- body rules: a slow call lexically inside a `with session_scope()` -------------
    for node in ast.walk(tree):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        if not any(_opens_session(item) for item in node.items):
            continue
        scope = _nearest_scope(node, owner, parents)
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if not isinstance(inner, ast.Call):
                    continue
                rule = _classify(inner)
                if rule is not None:
                    record(rule, inner, scope, 'runs with a DB transaction open')
        # A `with ThreadPoolExecutor(...)` inside the session body is caught above by its
        # constructor call; `as_completed`/`submit` inside it are the same block.

    # --- interprocedural rule: a function that ACCEPTS a session and does slow work ----
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        param = _takes_session(node)
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
        released_after = _release_line(node, param)
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            rule = _classify(inner)
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
                        f'{_dotted(inner.func) or _last_name(inner.func)}() runs inside a '
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


# ------------------------------------------------------------------------------- allowlist


def load_allowlist(path: Path = _ALLOWLIST_PATH) -> dict[str, list[str]]:
    """Map ``<file>::<scope>::<category>`` to the reason of EACH line carrying that key.

    A list, not a set: **one line buys one finding**. Set membership cannot see a PARTIAL
    fix — a key producing three findings and a key producing one would be the same key, so
    fixing two of three calls would leave the run green with the exemption still covering
    the third. Duplicate keys are therefore meaningful, not an error.
    """
    if not path.exists():
        return {}
    allowed: dict[str, list[str]] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, _, reason = line.partition('#')
        key = key.strip()
        if not key:
            continue
        allowed.setdefault(key, []).append(reason.strip())
    return allowed


def apply_allowlist(
    findings: list[Finding], allowed: dict[str, list[str]]
) -> tuple[list[Finding], list[Finding], int, list[str], list[str]]:
    """Split findings by allowlist coverage, one line per finding.

    Returns ``(unallowlisted, backlog, accepted, stale, unreasoned)``. ``stale`` names entries
    with no finding left to cover — either the key vanished or the file holds MORE lines than
    the key now produces. That is what makes "this file can only shrink" true per-finding
    rather than per-function. ``unreasoned`` names entries whose reason is missing: an
    exemption nobody had to justify is the one nobody will ever revisit.
    """
    by_key: dict[str, list[Finding]] = {}
    for finding in findings:
        by_key.setdefault(finding.key, []).append(finding)

    unallowlisted: list[Finding] = []
    backlog: list[Finding] = []
    accepted = 0
    for key, group in by_key.items():
        reasons = allowed.get(key, [])
        for _finding, reason in zip(group, reasons, strict=False):
            if reason.startswith(_BACKLOG_PREFIX):
                backlog.append(_finding)
            else:
                accepted += 1
        unallowlisted.extend(group[len(reasons) :])

    stale: list[str] = []
    for key, reasons in sorted(allowed.items()):
        live = len(by_key.get(key, []))
        if live >= len(reasons):
            continue
        if live == 0:
            stale.append(
                f'{key}  ({len(reasons)} entr{"y" if len(reasons) == 1 else "ies"}, 0 findings)'
            )
        else:
            stale.append(
                f'{key}  ({len(reasons)} entries, {live} finding(s) — delete {len(reasons) - live})'
            )

    unreasoned = sorted(key for key, reasons in allowed.items() if any(not r for r in reasons))
    return unallowlisted, backlog, accepted, stale, unreasoned


# ------------------------------------------------------------------------------- self-test

#: ``(category, source)`` — each source MUST produce the named category. A detector that
#: silently stops matching reports 0 findings, which reads exactly like a clean codebase.
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


def run_selftest(verbose: bool = True) -> list[str]:
    """Return failure descriptions — empty means every detector is alive."""
    failures: list[str] = []

    for category, source in SELFTEST_CASES:
        got = {f.category for f in scan_source(source, 'fixture.py')}
        ok = category in got
        if not ok:
            failures.append(f'{category} did not fire (got {sorted(got) or "nothing"})')
        if verbose:
            mark = '\033[32m✓' if ok else '\033[31m✗'
            print(f'  {mark}\033[0m fires {category}')

    for i, source in enumerate(SELFTEST_CLEAN, start=1):
        found = scan_source(source, 'fixture.py')
        if found:
            detail = ', '.join(f'{f.category}: {f.detail}' for f in found)
            failures.append(f'clean case {i} produced {detail}')
        if verbose:
            mark = '\033[31m✗' if found else '\033[32m✓'
            print(f'  {mark}\033[0m clean case {i} produces no finding')

    for category, source in SELFTEST_ONCE:
        hits = [f for f in scan_source(source, 'fixture.py') if f.category == category]
        if len(hits) != 1:
            failures.append(f'{category} fired {len(hits)} times, expected exactly 1')
        if verbose:
            mark = '\033[32m✓' if len(hits) == 1 else '\033[31m✗'
            print(f'  {mark}\033[0m {category} fires exactly once')

    return failures


def _selftest_main() -> int:
    print('\n\033[1maudit-session-lifetime self-test\033[0m\n')
    failures = run_selftest()
    if failures:
        print(f'\n\033[31m{len(failures)} self-test failure(s) — a detector is broken\033[0m')
        for line in failures:
            print(f'  {line}')
        print()
        return 1
    total = len(SELFTEST_CASES) + len(SELFTEST_CLEAN) + len(SELFTEST_ONCE)
    print(f'\n\033[32mall {total} self-test cases pass\033[0m\n')
    return 0


# ----------------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('root', type=Path, nargs='?', help='source tree to scan (e.g. backend/app)')
    ap.add_argument('--category', choices=CATEGORIES, help='limit to one detector')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--list', action='store_true', help='print every finding, not just counts')
    ap.add_argument(
        '--selftest', action='store_true', help='audit the auditor against in-memory fixtures'
    )
    args = ap.parse_args()

    if args.selftest:
        return _selftest_main()
    if args.root is None:
        ap.error('root is required unless --selftest is given')
    if not args.root.is_dir():
        print(f'error: {args.root} is not a directory', file=sys.stderr)
        return 2

    # A broken detector reports zero findings, so never let the tree scan speak without it.
    selftest_failures = run_selftest(verbose=False)

    findings: list[Finding] = []
    for path in sorted(args.root.rglob('*.py')):
        findings.extend(scan_file(path, args.root))

    if args.category:
        findings = [f for f in findings if f.category == args.category]

    allowed = load_allowlist()
    unallowed, backlog, accepted, all_stale, unreasoned = apply_allowlist(findings, allowed)
    stale = all_stale if not args.category else []

    if args.json:
        print(
            json.dumps(
                {
                    'total': len(findings),
                    'accepted': accepted,
                    'backlog': len(backlog),
                    'unallowlisted': len(unallowed),
                    'stale_allowlist_entries': stale,
                    'unreasoned_allowlist_entries': unreasoned,
                    'selftest_failures': selftest_failures,
                    'by_category': dict(Counter(f.category for f in unallowed)),
                    'backlog_by_category': dict(Counter(f.category for f in backlog)),
                    'findings': [f.__dict__ for f in unallowed],
                },
                indent=2,
            )
        )
        return 1 if (unallowed or stale or unreasoned or selftest_failures) else 0

    counts = Counter(f.category for f in findings)
    backlog_counts = Counter(f.category for f in backlog)
    print(
        f'\n\033[1m{args.root}\033[0m — {len(findings)} findings: '
        f'{len(unallowed)} open, {len(backlog)} backlog, {accepted} accepted\n'
    )
    for category in CATEGORIES:
        hits = [f for f in unallowed if f.category == category]
        total = counts.get(category, 0)
        colour = '\033[31m' if hits else '\033[32m'
        deferred = backlog_counts.get(category, 0)
        suffix = f', {deferred} backlog' if deferred else ''
        print(f'  {colour}{category:26s}\033[0m {len(hits):4d} open  ({total} total{suffix})')
        if args.list or len(hits) <= 5:
            for f in hits[: (None if args.list else 5)]:
                print(f'      {f.path}:{f.line} {f.scope} — {f.detail}')
        elif hits:
            print(f'      … {len(hits)} findings (--list)')

    if selftest_failures:
        print(f'\n\033[31mSELF-TEST BROKEN — {len(selftest_failures)} detector(s) dead:\033[0m')
        for line in selftest_failures:
            print(f'  {line}')
        print('  The counts above are not trustworthy. Run --selftest.\n')
    if unreasoned:
        print(f'\n\033[31m{len(unreasoned)} allowlist entr(y/ies) carry NO reason:\033[0m')
        for key in unreasoned[:20]:
            print(f'  {key}')
        print(
            '  Every exemption needs a written reason — one nobody had to justify is one\n'
            '  nobody will ever revisit.\n'
        )
    if stale:
        print(f'\n\033[31m{len(stale)} allowlist key(s) hold more entries than findings:\033[0m')
        for key in stale[:20]:
            print(f'  {key}')
        if len(stale) > 20:
            print(f'  … {len(stale) - 20} more')
        print(f'  Delete the surplus lines from {_ALLOWLIST_PATH}. One line buys one finding:')
        print('  a stale exemption is a blanket nobody reviews, and a surplus one keeps')
        print('  covering the calls you did NOT move out of the transaction.\n')
    if backlog:
        print(
            f'\033[1;33m{len(backlog)} finding(s) are DEFERRED WORK, not accepted patterns.\033[0m'
        )
        print(
            f'  They carry a `{_BACKLOG_PREFIX}` reason in {_ALLOWLIST_PATH}. This gate is\n'
            '  green because nothing NEW landed — not because the tree is clean.'
        )
    if unallowed:
        print(f'\n\033[31m{len(unallowed)} findings need a fix or an allowlist entry\033[0m')
        print(f'  allowlist: {_ALLOWLIST_PATH}')
        print('  (one "<file>::<scope>::<category>  # reason" per line)')
        print('  The fix pattern is in backend/app/tasks/CLAUDE.md: a short read session')
        print('  returning PLAIN DATA, the slow work with NO session, then a short write.\n')
    if unallowed or stale or unreasoned or selftest_failures:
        return 1
    print('\n\033[32mno un-allowlisted findings\033[0m\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
