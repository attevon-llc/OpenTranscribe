"""``python -m app.scripts.corpus_injection --purge`` against a real Postgres (issue #1222).

The injector wrote rows with no way to take them back out: the only removal was each test's
hand-rolled ORM teardown, which skipped OpenSearch chunks, speaker embeddings and clusters.
``--purge`` hands every injected file to ``purge_media_file`` (the destroy all delete paths
share). What these tests pin is the SELECTION, since a purge that is too wide deletes a
user's library and one that is too narrow leaks the corpus:

* only the named (corpus, seed) namespace goes;
* a row that merely carries a ``rag_eval`` block whose uuid is not the derived one stays;
* ``--dry-run`` deletes nothing and a bare ``--purge`` (no ``--yes``) refuses.

Point it at an isolated stack, never the shared dev one (the CLI refuses the dev ports).
"""

from __future__ import annotations

import os
import uuid
from unittest import mock

import pytest
from fixtures.synthetic_corpus import write_fixture_corpus
from sqlalchemy import func
from sqlalchemy import select

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.scripts.corpus_injection.__main__ import main as _injector_main
from app.scripts.corpus_injection.adapters.synthetic import SyntheticAdapter
from app.scripts.corpus_injection.injector import inject_meeting
from app.scripts.corpus_injection.purge import find_injected_files
from app.scripts.corpus_injection.purge import purge_injected

_STACK_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _STACK_ABSENT,
        reason="No stack reachable (SKIP_OPENSEARCH): purge destroys through every store.",
    ),
]

CORPUS = "synthetic"
RUN = uuid.uuid4().hex[:10]
SEED_A = f"pytest-synth-{RUN}"
SEED_B = f"pytest-synth-{uuid.uuid4().hex[:10]}"
SEED_C = f"pytest-synth-{uuid.uuid4().hex[:10]}"  # the lookalike's original seed


@pytest.fixture(scope="module")
def engine():
    from app.db.base import engine as app_engine

    return app_engine


@pytest.fixture
def session(engine):
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        yield db


@pytest.fixture
def owner(session):
    from app.models.user import User

    user = session.execute(select(User).order_by(User.id).limit(1)).scalar_one_or_none()
    if user is None:
        pytest.skip("No user rows on the target stack; nothing can own an injected file.")
    return user


@pytest.fixture
def docs(tmp_path):
    adapter = SyntheticAdapter(write_fixture_corpus(tmp_path / "synthetic"), meetings=0)
    return [adapter.load(m) for m in adapter.meeting_ids()]


@pytest.fixture
def injected(session, owner, docs):
    """Inject the fixture meetings under SEED_A and (first meeting only) SEED_B."""
    lookalike_uuid = None
    try:
        for doc in docs:
            inject_meeting(session, doc, owner.id, seed=SEED_A, tool_version="pytest")
            session.commit()
        inject_meeting(session, docs[0], owner.id, seed=SEED_B, tool_version="pytest")
        session.commit()

        # A hand-built row that carries SEED_A's metadata block but not the derived uuid.
        record, _ = inject_meeting(session, docs[0], owner.id, seed=SEED_C, tool_version="pytest")
        session.commit()
        lookalike_uuid = uuid.uuid4()
        row = session.get(MediaFile, record.media_file_id)
        block = dict(row.metadata_important["rag_eval"])
        block["seed"] = SEED_A
        row.metadata_important = {"rag_eval": block}
        row.uuid = lookalike_uuid
        session.commit()
        yield lookalike_uuid
    finally:
        session.rollback()
        for seed in (SEED_A, SEED_B, SEED_C):
            purge_injected(session, owner.id, CORPUS, seed)
        if lookalike_uuid is not None:
            stray = session.execute(
                select(MediaFile).where(MediaFile.uuid == lookalike_uuid)
            ).scalar_one_or_none()
            if stray is not None:
                from app.services.file_cleanup_service import purge_media_file

                purge_media_file(session, stray)


def _count(session, seed: str) -> int:
    from app.scripts.corpus_injection.rows import RAG_EVAL_KEY

    return int(
        session.scalar(
            select(func.count(MediaFile.id)).where(
                MediaFile.metadata_important[RAG_EVAL_KEY]["seed"].as_string() == seed
            )
        )
        or 0
    )


def test_selection_is_the_named_namespace_and_uuid_verified(session, owner, docs, injected):
    found = find_injected_files(session, owner.id, CORPUS, SEED_A)
    assert {str(f.uuid) for f in found}.isdisjoint({str(injected)}), "a lookalike was selected"
    assert len(found) == len(docs)
    assert find_injected_files(session, owner.id, CORPUS, "no-such-seed") == []
    assert find_injected_files(session, owner.id, "qmsum", SEED_A) == []


def test_dry_run_deletes_nothing(session, owner, docs, injected):
    report = purge_injected(session, owner.id, CORPUS, SEED_A, dry_run=True)
    assert len(report.matched) == len(docs)
    assert report.deleted == []
    assert len(find_injected_files(session, owner.id, CORPUS, SEED_A)) == len(docs)


def test_purge_removes_the_namespace_and_nothing_else(session, owner, docs, injected):
    ids_a = [f.id for f in find_injected_files(session, owner.id, CORPUS, SEED_A)]
    report = purge_injected(session, owner.id, CORPUS, SEED_A)
    assert report.clean, report.failed
    assert len(report.deleted) == len(docs)

    session.expire_all()
    assert find_injected_files(session, owner.id, CORPUS, SEED_A) == []
    assert not session.scalar(select(MediaFile.id).where(MediaFile.id.in_(ids_a)).limit(1))
    assert not session.scalar(
        select(TranscriptSegment.id).where(TranscriptSegment.media_file_id.in_(ids_a)).limit(1)
    )
    assert not session.scalar(select(Speaker.id).where(Speaker.media_file_id.in_(ids_a)).limit(1))
    # The other seed and the uuid-mismatched lookalike are untouched.
    assert len(find_injected_files(session, owner.id, CORPUS, SEED_B)) == 1
    assert session.scalar(select(MediaFile.id).where(MediaFile.uuid == injected)) is not None


def injector_cli(argv: list[str]) -> int:
    """Run the corpus-injection CLI against the test stack without leaking its environment.

    ``--allow-live-stack``: this suite's database IS the stack the CLI's port guard would
    otherwise refuse (exit 2, indistinguishable from the missing ``--yes`` refusal asserted
    below). ``bootstrap()`` also writes service hosts into ``os.environ`` (``REDIS_HOST=redis``
    and friends), which would otherwise outlive this test and break every later one.
    """
    with mock.patch.dict(os.environ):
        return _injector_main([*argv, "--allow-live-stack"])


def test_cli_refuses_without_yes_then_purges_with_it(session, owner, docs, injected):
    base = ["--corpus", CORPUS, "--user", owner.email, "--seed", SEED_B, "--purge"]
    assert injector_cli(base) == 2
    assert len(find_injected_files(session, owner.id, CORPUS, SEED_B)) == 1

    assert injector_cli([*base, "--dry-run"]) == 0
    assert len(find_injected_files(session, owner.id, CORPUS, SEED_B)) == 1

    assert injector_cli([*base, "--yes"]) == 0
    session.expire_all()
    assert find_injected_files(session, owner.id, CORPUS, SEED_B) == []
    assert _count(session, SEED_A) == len(docs) + 1  # SEED_A files + the lookalike
