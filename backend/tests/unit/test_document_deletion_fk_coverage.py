"""Every foreign key into ``document`` the DATABASE won't sweep on its own (#362 lane C3/C4).

Same defect class ``test_user_deletion_fk_coverage.py`` guards for ``user``/``media_file``/
``speaker``, scoped to ``document`` instead: a table gains a foreign key into
``document.id``, that FK is anything other than ``ON DELETE CASCADE``, and nobody notices —
so ``DELETE /documents/{uuid}`` (an ORM instance delete, ``documents.py::delete_document``)
or ``gdpr_erasure_service._purge_documents``'s ``db.delete(document)`` starts raising
``ForeignKeyViolation`` the day the new table's first row exists.

``document_chunk.document_id`` (v394) and ``file_facts.document_id`` (v398) are both
``ON DELETE CASCADE`` today, which is why nothing has broken yet — this test exists so the
NEXT one is a deliberate decision instead of an accident, the same reasoning the module
docstring on the ``user``/``media_file``/``speaker`` version gives for deriving from the
live schema rather than trusting a comment to stay current.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

#: Postgres ``pg_constraint.confdeltype`` codes, spelled out (matches
#: ``test_user_deletion_fk_coverage.py``'s convention).
CASCADE = "c"
NO_ACTION = "a"
SET_NULL = "n"


@dataclass(frozen=True)
class Disposition:
    """How one non-CASCADE foreign key into ``document`` is accounted for.

    Attributes:
        rule: Expected ``confdeltype`` (:data:`NO_ACTION` or :data:`SET_NULL` — a
            document-child FK is never expected to need ``CASCADE`` registered here,
            because CASCADE needs no help from anybody).
        swept_by: Dotted function that must run before a document delete, or that
            makes the FK harmless another way. Free text, not machine-checked — the
            enforcement here is that an entry EXISTS and gives a reason, not that the
            dotted path resolves.
        reason: Mandatory. Why the FK is shaped this way.
    """

    rule: str
    swept_by: str
    reason: str


#: A NO_ACTION or SET_NULL foreign key into ``document.id`` appearing in the live schema
#: WITHOUT a matching entry here is exactly the defect this test exists to catch — see
#: ``test_every_non_cascade_document_fk_is_registered``.
_REGISTERED: dict[str, Disposition] = {
    "task.document_id": Disposition(
        NO_ACTION,
        "app.models.document.Document.tasks (ORM delete-orphan)",
        "v399 (#362 lane C3/C4). A task row is cheap history, not worth a DB-level "
        "CASCADE decision — same house rule as task.media_file_id. Document.tasks "
        "declares cascade='all, delete-orphan', which fires on BOTH instance-delete "
        "call sites this table has: documents.py::delete_document (API) and "
        "gdpr_erasure_service._purge_documents (Art. 17). Neither does a bulk "
        "query(Document).delete(), so unlike MediaFile's admin bulk-delete path, "
        "there is no second sweep to also register.",
    ),
    "watch_source_file.document_id": Disposition(
        SET_NULL,
        "<db-set-null> — no app-level sweep needed",
        "v395_add_watch_source_file_document_id (#362). watch_source_file is the "
        "auto-import ledger row, not owned by the document — it survives the document "
        "it produced (e.g. re-import history) and the DATABASE clears the back-"
        "reference on delete, same as media_file's mirror column on the same table.",
    ),
}


def _live_document_child_fks(conn) -> dict[str, str]:
    """``{"<table>.<column>": confdeltype}`` for every FK referencing ``document.id``.

    Guarded the same way ``test_user_deletion_fk_coverage.py`` guards its own derivation:
    a query that matches nothing would make every assertion below vacuously pass, so
    :func:`test_the_deriving_query_finds_the_known_cascade_children` requires it to find
    the two FKs known to exist today before anything else in this module is trusted.
    """
    rows = conn.execute(
        text(
            "SELECT tc.relname, a.attname, c.confdeltype "
            "FROM pg_constraint c "
            "JOIN pg_class tc ON tc.oid = c.conrelid "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype = 'f' "
            "AND c.confrelid = 'document'::regclass"
        )
    )
    return {f"{row[0]}.{row[1]}": row[2] for row in rows}


def test_the_deriving_query_finds_the_known_cascade_children(db_session):
    """Guard the guard: if this finds nothing, every test below is vacuous."""
    live = _live_document_child_fks(db_session.connection())
    assert "document_chunk.document_id" in live
    assert "file_facts.document_id" in live
    assert "task.document_id" in live
    assert live["document_chunk.document_id"] == CASCADE
    assert live["file_facts.document_id"] == CASCADE
    assert live["task.document_id"] == NO_ACTION


def test_every_non_cascade_document_fk_is_registered(db_session):
    """The failure mode this module exists to catch, made concrete.

    A future migration that adds e.g. ``document_annotation.document_id`` with the
    default ``NO ACTION`` rule and never updates ``_REGISTERED`` (or the delete paths
    it documents) makes THIS assertion fail — before the first delete of a document
    with an annotation raises in production.
    """
    live = _live_document_child_fks(db_session.connection())
    unregistered = {
        key: rule for key, rule in live.items() if rule != CASCADE and key not in _REGISTERED
    }
    assert not unregistered, (
        f"{unregistered} reference document.id without ON DELETE CASCADE and are not "
        "registered in _REGISTERED — add a Disposition explaining what sweeps them "
        "before a document can be deleted, in both documents.py::delete_document and "
        "gdpr_erasure_service, matching the pattern test_user_deletion_fk_coverage.py "
        "already uses for user/media_file/speaker."
    )


def test_registered_dispositions_match_the_live_rule(db_session):
    """A migration that silently narrows a registered FK's rule must fail here too."""
    live = _live_document_child_fks(db_session.connection())
    for key, disposition in _REGISTERED.items():
        assert key in live, f"{key} is registered but no longer exists in the live schema"
        assert live[key] == disposition.rule, (
            f"{key} is registered as {disposition.rule!r} but the database enforces {live[key]!r}"
        )


def test_the_orm_delete_orphan_cascade_actually_sweeps_task_rows(db_session):
    """``task.document_id`` has no DB-level CASCADE, so this is the ONLY thing that
    sweeps it — verified by an ORM instance delete, not a claim about the relationship.
    """
    import uuid as uuid_pkg

    from app.core.security import get_password_hash
    from app.models.document import Document
    from app.models.media import Task
    from app.models.user import User

    db = db_session
    user = User(
        email=f"fkcov_{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password=get_password_hash("x"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()

    document = Document(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="fkcov.pdf",
        storage_path="x/fkcov.pdf",
        file_size=1,
        content_type="application/pdf",
    )
    db.add(document)
    db.commit()

    task = Task(
        id=f"fkcov-{uuid_pkg.uuid4().hex[:10]}",
        user_id=user.id,
        document_id=document.id,
        task_type="document_parse",
        status="pending",
    )
    db.add(task)
    db.commit()
    task_id, document_id = task.id, document.id

    db.delete(document)
    db.commit()

    assert db.query(Task).filter(Task.id == task_id).first() is None
    assert db.query(Document).filter(Document.id == document_id).first() is None


def test_document_chunk_and_file_facts_cascade_verified_by_execution(db_session):
    """Same spirit as the parent suite: prove the CASCADE by deleting a row, not just
    by reading ``confdeltype``.
    """
    import uuid as uuid_pkg

    conn = db_session.connection()
    try:
        user_id = conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"fkcov_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
        document_id = conn.execute(
            text(
                "INSERT INTO document (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, 'fkcov.pdf', 'x/fkcov.pdf', 1, "
                "'application/pdf') RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO document_chunk (document_id, chunk_index, text, char_start, "
                "char_end, section_path, block_types) VALUES "
                "(:d, 0, 'hello', 0, 5, '[]'::jsonb, '[]'::jsonb)"
            ),
            {"d": document_id},
        )
        conn.execute(
            text(
                "INSERT INTO file_facts (document_id, generator_version, source_fingerprint, "
                "facts, digest, keyphrases, digest_word_count, section_count, generation_ms) "
                "VALUES (:d, '1.0.0', :fp, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 1, 1, 1)"
            ),
            {"d": document_id, "fp": "1" * 64},
        )

        conn.execute(text("DELETE FROM document WHERE id = :d"), {"d": document_id})

        assert (
            conn.execute(
                text("SELECT count(*) FROM document_chunk WHERE document_id = :d"),
                {"d": document_id},
            ).scalar()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM file_facts WHERE document_id = :d"), {"d": document_id}
            ).scalar()
            == 0
        )
    finally:
        db_session.rollback()
