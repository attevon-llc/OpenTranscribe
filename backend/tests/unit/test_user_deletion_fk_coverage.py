"""Every foreign key into ``user`` / ``media_file`` / ``speaker`` the DATABASE won't sweep.

The defect this exists to catch
-------------------------------
``api/endpoints/admin._delete_user_owned_records`` and
``services/gdpr_erasure_service._delete_owner_scoped_rows`` are two independent,
hand-maintained lists of the foreign keys that have no ``ON DELETE CASCADE``.
Nothing in the application compares them, neither is derived from the schema, and
both are the *only* thing standing between a new table and a permanently broken
account deletion:

* ``DELETE /api/admin/users/{uuid}`` wraps the cascade in ``except Exception`` and
  answers ``500 "User deletion failed"`` — the constraint that refused is never
  named, in the response or the audit trail.
* ``gdpr_erasure_service.erase_user`` never raises: it records the failure in
  ``summary["errors"]`` and audits the erasure as ``PARTIAL``. An Art. 17 request
  that did not complete looks, from the API, like one that did.

``v386`` added four foreign keys into ``user`` and one into ``tag`` in a single
revision. The next such table breaks user deletion with no test going red — unless
the set of foreign keys is *derived* rather than remembered, which is what this
module does. Same pattern as ``test_ddl_marker_discipline.py`` and
``test_e2e_data_hygiene.py``: derive the facts, require every one to be registered
with a written reason, and guard the deriving query itself so one that matches
nothing cannot pass everything.

It has already earned it. Writing this module surfaced a live break: ``speaker`` is a
third parent the deletion path deletes from, ``transcript_segment.speaker_id`` is
``ON DELETE NO ACTION``, and ``_delete_user_speakers`` runs BEFORE the segments are
removed — so deleting any account with a diarized file raised
``ForeignKeyViolation`` on ``transcript_segment_speaker_id_fkey`` (every one of the
14,274 segments in the dev database carries a ``speaker_id``). That is why the parent set
below is three tables, not two.

Why the derivation is "not CASCADE" rather than "NO ACTION"
-----------------------------------------------------------
``ON DELETE CASCADE`` is the one rule that needs no help from anybody. Every other
rule — ``NO ACTION``, ``RESTRICT``, ``SET NULL``, ``SET DEFAULT`` — means either
some code has to act first, or the row deliberately survives with a column blanked.
Both of those are decisions, and a decision needs a reason on record. Deriving on
"not CASCADE" also keeps the registry's KEY set stable when a migration changes a
*rule* (``v387`` moved five FKs from ``NO ACTION`` to ``SET NULL``): the entry stays,
its ``rule`` field changes, and :func:`test_the_declared_delete_rules_match_the_live_schema`
is what goes red on a database that has not run the migration yet.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from dataclasses import dataclass

import pytest
from sqlalchemy import text

#: Postgres ``pg_constraint.confdeltype`` codes, spelled out.
NO_ACTION = "a"
SET_NULL = "n"

#: The tables the two deletion paths issue DELETEs against and whose children therefore
#: have to be dealt with first. ``speaker`` is here because leaving it out is what hid a
#: live break (see the module docstring); ``tag``, ``collection`` and
#: ``speaker_collection`` are deliberately absent — every FK into them is already
#: ``ON DELETE CASCADE``, verified by
#: :func:`test_no_other_deletion_parent_has_grown_a_non_cascade_child`.
_PARENT_TABLES = ("user", "media_file", "speaker")

#: The three non-code mechanisms a path may claim instead of naming a helper. Each is
#: MACHINE-CHECKED below, because "the database handles it" is precisely the kind of
#: claim that is true right up until a migration changes the rule.
#:
#: ``DB_SET_NULL``            the FK's own ``ON DELETE SET NULL``; checked against
#:                            ``pg_constraint``.
#: ``DB_CASCADE_VIA_SIBLING`` a *different* FK on the same child table cascades, so the
#:                            row is gone before this one is consulted; checked by
#:                            requiring that sibling to exist and be ``CASCADE``.
#: ``ORM_CASCADE``            ``MediaFile``'s ``delete-orphan`` relationship, which fires
#:                            only for an INSTANCE delete (``db.delete(file)``); checked
#:                            against the SQLAlchemy mapper. A bulk
#:                            ``query(MediaFile).delete()`` does NOT fire it, which is
#:                            why the admin path cannot claim this and the GDPR path can.
DB_SET_NULL = "<db-set-null>"
DB_CASCADE_VIA_SIBLING = "<db-cascade-via-sibling-fk>"
ORM_CASCADE = "<orm-delete-orphan-on-instance-delete>"
_SENTINELS = frozenset({DB_SET_NULL, DB_CASCADE_VIA_SIBLING, ORM_CASCADE})

_ADMIN_OWNED = "app.api.endpoints.admin._delete_user_owned_records"
_ADMIN_SPEAKERS = "app.api.endpoints.admin._delete_user_speakers"
_ADMIN_FILES = "app.api.endpoints.admin._delete_user_media_files"
_GDPR_ROWS = "app.services.gdpr_erasure_service._delete_owner_scoped_rows"
_GDPR_FILES = "app.services.gdpr_erasure_service._purge_files"


@dataclass(frozen=True)
class Disposition:
    """How one non-CASCADE foreign key is accounted for, and why that is right.

    Attributes:
        rule: Expected ``confdeltype`` — ``NO_ACTION`` (code must clear the rows) or
            ``SET_NULL`` (the row survives, de-attributed).
        model: SQLAlchemy class name. Each named handler's source must mention it, so
            deleting the branch fails this suite rather than only the endpoint.
        admin_path: Dotted handler in the ``DELETE /admin/users/{uuid}`` path, or one
            of the three machine-checked sentinels (:data:`DB_SET_NULL`,
            :data:`DB_CASCADE_VIA_SIBLING`, :data:`ORM_CASCADE`).
        gdpr_path: Same, for ``gdpr_erasure_service.erase_user``.
        reason: Mandatory. What this FK records and why the two paths are shaped the
            way they are. An entry without one is a to-do disguised as coverage.
    """

    rule: str
    model: str
    admin_path: str
    gdpr_path: str
    reason: str


#: Owner-scoped FKs: the subject IS the ``user_id``, so a ``WHERE user_id = :id``
#: sweep finds them and both deletion paths must run one.
_OWNER_SCOPED: dict[str, Disposition] = {
    "transcript_segment.speaker_id": Disposition(
        NO_ACTION,
        "TranscriptSegment",
        _ADMIN_SPEAKERS,
        ORM_CASCADE,
        "The FK that was actually broken. Nullable, NO ACTION, and every diarized "
        "segment sets it — so the admin path had to DETACH the segments before "
        "_delete_user_speakers removed the speakers, because the segments are not "
        "deleted until the later _delete_user_media_files pass. GDPR is unaffected: "
        "purge_media_file's instance delete takes the segments with the file, before "
        "anything touches the speakers.",
    ),
    "media_file.user_id": Disposition(
        NO_ACTION,
        "MediaFile",
        _ADMIN_FILES,
        _GDPR_FILES,
        "The files themselves. GDPR goes per-file through purge_media_file so storage "
        "and OpenSearch are cleaned too; admin bulk-deletes the rows.",
    ),
    "transcript_segment.media_file_id": Disposition(
        NO_ACTION,
        "TranscriptSegment",
        _ADMIN_FILES,
        ORM_CASCADE,
        "The asymmetry at the heart of this table: MediaFile declares delete-orphan, but "
        "the FK is NO ACTION, so only an INSTANCE delete sweeps it. GDPR's "
        "purge_media_file does db.delete(file) and gets it free; admin ends in a bulk "
        "query(MediaFile).delete() that fires no ORM cascade, hence the explicit pass.",
    ),
    "analytics.media_file_id": Disposition(
        NO_ACTION,
        "Analytics",
        _ADMIN_FILES,
        ORM_CASCADE,
        "Same delete-orphan / NO ACTION split as transcript_segment: one analytics row "
        "per file, invisible once the file is gone but still holding its FK.",
    ),
    "comment.media_file_id": Disposition(
        NO_ACTION,
        "Comment",
        _ADMIN_FILES,
        ORM_CASCADE,
        "Commenting is collaborative (viewer+ on a shared file), so a comment on this "
        "user's file may belong to ANOTHER account — it must be scoped by file, not by "
        "author, or it blocks the bulk MediaFile delete.",
    ),
    "task.media_file_id": Disposition(
        NO_ACTION,
        "Task",
        _ADMIN_FILES,
        ORM_CASCADE,
        "Same shape as comment.media_file_id. Scoping only by task.user_id happens to "
        "work today because tasks are created with the file owner's id, which is a "
        "coincidence and not a constraint.",
    ),
    "comment.user_id": Disposition(
        NO_ACTION,
        "Comment",
        _ADMIN_OWNED,
        _GDPR_ROWS,
        "Comments this user wrote on OTHER people's files. No per-file pass can reach "
        "them, and the FK is NOT NULL, so they block the user-row delete.",
    ),
    "task.user_id": Disposition(
        NO_ACTION,
        "Task",
        _ADMIN_OWNED,
        _GDPR_ROWS,
        "media_file_id is nullable, so a task need not hang off any file.",
    ),
    "collection.user_id": Disposition(
        NO_ACTION,
        "Collection",
        _ADMIN_OWNED,
        _GDPR_ROWS,
        "An empty collection shell survives file cleanup; nothing else removes it.",
    ),
    "speaker.user_id": Disposition(
        NO_ACTION,
        "Speaker",
        _ADMIN_SPEAKERS,
        DB_CASCADE_VIA_SIBLING,
        "Admin needs its own pass because it collects the UUIDs first, to clear the "
        "OpenSearch embeddings the bulk SQL delete cannot trigger. GDPR reaches every "
        "speaker through speaker.media_file_id, which IS ON DELETE CASCADE, fired by "
        "purge_media_file's instance delete.",
    ),
    "speaker_profile.user_id": Disposition(
        NO_ACTION,
        "SpeakerProfile",
        _ADMIN_OWNED,
        _GDPR_ROWS,
        "purge_media_file deliberately PRESERVES speaker profiles, so erasure must "
        "remove them explicitly — and clear their profile embedding first.",
    ),
    "speaker_collection.user_id": Disposition(
        NO_ACTION,
        "SpeakerCollection",
        _ADMIN_OWNED,
        _GDPR_ROWS,
        "Owner-scoped shell, like Collection: purge_media_file never touches it, because "
        "a speaker collection groups PROFILES rather than files.",
    ),
    "tag.user_id": Disposition(
        NO_ACTION,
        "Tag",
        _ADMIN_OWNED,
        _GDPR_ROWS,
        "v374. Nullable because user_id IS NULL means a SYSTEM tag, which is shared "
        "vocabulary and must never be deleted with an account. Its file_tag rows are "
        "detached first: one may hang off another user's file.",
    ),
}

#: Actor FKs: the row belongs to somebody else and records what THIS user did to it.
#: No ``WHERE user_id = :id`` sweep can find them, which is why the database rule is
#: the handler (``v387``) rather than a sixth entry in each hand-maintained list.
_ACTOR: dict[str, Disposition] = {
    "auth_config_audit.changed_by": Disposition(
        SET_NULL,
        "AuthConfigAudit",
        DB_SET_NULL,
        DB_SET_NULL,
        "v387, and it lost NOT NULL to get here. Deleting the record of a change "
        "because its author left is the opposite of an audit trail; "
        "auth_config.get_audit_log already renders a missing actor as unknown.",
    ),
    "auth_config.created_by": Disposition(
        SET_NULL,
        "AuthConfig",
        DB_SET_NULL,
        DB_SET_NULL,
        "v387. The configured auth method must outlive the admin who configured it — "
        "same rule scim_token.created_by has always had.",
    ),
    "auth_config.updated_by": Disposition(
        SET_NULL,
        "AuthConfig",
        DB_SET_NULL,
        DB_SET_NULL,
        "v387. Twin of created_by: the last editor of a working auth method is not a "
        "reason the method has to stop working when they leave.",
    ),
    "media_file.quarantined_by": Disposition(
        SET_NULL,
        "MediaFile",
        DB_SET_NULL,
        DB_SET_NULL,
        "v387. A takedown never deletes rows, so the file belongs to a different "
        "account and survives; only the reviewer's attribution goes.",
    ),
    "summary_prompt.shared_by": Disposition(
        SET_NULL,
        "SummaryPrompt",
        DB_SET_NULL,
        DB_SET_NULL,
        "v387. prompts.share_prompt accepts owner OR admin, so this points at a row "
        "the owner-scoped sweep never matches.",
    ),
}

#: Already ``SET NULL`` before this suite existed, for the same "the artifact outlives
#: its author" reason. Registered so the set is complete and so a future migration
#: cannot quietly turn one back into ``NO ACTION`` without a failure here.
_PRE_EXISTING_SET_NULL: dict[str, Disposition] = {
    "user.approved_by": Disposition(
        SET_NULL,
        "User",
        DB_SET_NULL,
        DB_SET_NULL,
        "v381. The approval stands after the approver leaves; approval_status is the "
        "column that gates login, and approved_by is only its attribution.",
    ),
    "scim_token.created_by": Disposition(
        SET_NULL,
        "SCIMToken",
        DB_SET_NULL,
        DB_SET_NULL,
        "v382, and documented as such in models/CLAUDE.md: provisioning survives the "
        "issuing admin's departure, or an IdP integration dies with a personnel change.",
    ),
    "email_notification_config.created_by": Disposition(
        SET_NULL,
        "EmailNotificationConfig",
        DB_SET_NULL,
        DB_SET_NULL,
        "Mail config outlives its author — and it is what delivers password resets, so "
        "losing it with an admin account would break account recovery for everyone.",
    ),
    "watch_source.created_by": Disposition(
        SET_NULL,
        "WatchSource",
        DB_SET_NULL,
        DB_SET_NULL,
        "The watch source's OWNER is watch_source.user_id (CASCADE); created_by is "
        "only who set it up.",
    ),
    "user_invitation.created_user_id": Disposition(
        SET_NULL,
        "UserInvitation",
        DB_SET_NULL,
        DB_SET_NULL,
        "The invitation record outlives the account it produced, which is the point of "
        "keeping it: it is the audit trail for how that account came to exist.",
    ),
    "usage_event.user_id": Disposition(
        SET_NULL,
        "UsageEvent",
        DB_SET_NULL,
        DB_SET_NULL,
        "Metering rows are financial records; the aggregate must not change because an "
        "account was deleted.",
    ),
    "usage_event.file_id": Disposition(
        SET_NULL,
        "UsageEvent",
        DB_SET_NULL,
        DB_SET_NULL,
        "Same as usage_event.user_id: the cost was incurred and stands after the file it "
        "was incurred on is deleted.",
    ),
    "watch_source_file.media_file_id": Disposition(
        SET_NULL,
        "WatchSourceFile",
        DB_SET_NULL,
        DB_SET_NULL,
        "The import-dedup ledger must keep remembering a file it already imported, "
        "or the watcher re-imports it.",
    ),
}

REGISTRY: dict[str, Disposition] = {**_OWNER_SCOPED, **_ACTOR, **_PRE_EXISTING_SET_NULL}

#: Divergences between the two hand-maintained owner-scoped lists, each with the
#: mechanism that makes it correct. Every model deleted by one and not the other must
#: appear here. Format: model name -> reason.
_REGISTERED_LIST_DIVERGENCES: dict[str, str] = {
    "SpeakerCollectionMember": (
        "admin only. It bulk-deletes the parent SpeakerCollection with "
        "query(...).delete(), which fires no ORM cascade, so the members must go first. "
        "GDPR deletes the parent as an instance and delete-orphan sweeps them."
    ),
    "CollectionMember": "admin only, same parent-instance-vs-bulk mechanism as SpeakerCollectionMember.",
    "SummaryPrompt": (
        "admin only, and redundant there: summary_prompt.user_id is ON DELETE CASCADE, "
        "so the user-row delete sweeps it in both paths. Kept because it also makes the "
        "intent legible next to the shared_by FK, which is NOT owner-scoped."
    ),
    "ChatConversation": (
        "GDPR only, and deliberately: chat threads quote transcript content back to the "
        "user, so they must be erased even when a legal hold keeps the user row (and "
        "therefore the chat_conversation.user_id CASCADE) from ever firing. Admin's path "
        "has no legal-hold branch — it always deletes the user row — so the CASCADE "
        "always runs there."
    ),
}


_NON_CASCADE_FK_SQL = """
    SELECT c.conrelid::regclass::text AS child, a.attname AS col, c.confdeltype AS rule
    FROM pg_constraint c
    JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    WHERE c.contype = 'f'
      AND c.confrelid = ANY(CAST(:parents AS regclass[]))
      AND c.confdeltype <> 'c'
"""


def _live_non_cascade_fks(db_session, parents: tuple[str, ...] = _PARENT_TABLES) -> dict[str, str]:
    """``"table.column" -> confdeltype`` for every non-CASCADE FK into ``parents``.

    Derived from ``pg_constraint``, not from the models: Alembic is the schema
    authority and a model may lag it (``models/media.py`` has no ``ondelete`` on
    ``quarantined_by`` even though the database does).
    """
    rows = (
        db_session.connection()
        .execute(
            text(_NON_CASCADE_FK_SQL),
            # Quoted because `user` is a reserved word; regclass needs it that way.
            {"parents": "{" + ",".join(f'"{name}"' for name in parents) + "}"},
        )
        .all()
    )
    # regclass renders the reserved word as '"user"'; the registry keys are unquoted.
    return {row.child.strip('"') + "." + row.col: row.rule for row in rows}


def _handler_source(dotted: str) -> str:
    """Source text of a registered handler, resolved from its dotted path."""
    module_name, _, func_name = dotted.rpartition(".")
    module = importlib.import_module(module_name)
    return inspect.getsource(getattr(module, func_name))


def _referenced_model_names(dotted: str) -> set[str]:
    """Mapped model classes a deletion helper names anywhere in its body.

    Every bare name in the function is resolved through the **defining module's
    globals** and kept if it is a mapped class. Two reasons it is not narrowed to
    ``db.query(X)``:

    * ``admin.py`` imports ``Task as TaskModel``, so comparing the two lists on their
      *local* names would report a divergence that does not exist. Resolution through
      the module fixes that.
    * ``gdpr_erasure_service`` deletes three of its models through a
      ``for model, key in ((SpeakerCollection, ...), ...)`` loop, where the class
      appears only in a tuple literal and ``db.query(model)`` names a loop variable.
      A scan keyed on the call would find none of them, and the comparison would
      report a wholly fictional divergence.

    In these two functions every model mentioned is a model being deleted, so a
    mention is the right signal.
    """
    module_name, _, func_name = dotted.rpartition(".")
    module = importlib.import_module(module_name)
    tree = ast.parse(textwrap.dedent(_handler_source(dotted)))

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name):
            continue
        resolved = getattr(module, node.id, None)
        if isinstance(resolved, type) and hasattr(resolved, "__tablename__"):
            found.add(resolved.__name__)
    return found


def test_every_non_cascade_fk_into_user_or_media_file_is_registered(db_session):
    """Set equality, both directions — the assertion the whole module is for.

    A NEW foreign key with no ``ON DELETE CASCADE`` is unregistered and fails here
    *before* it silently breaks account deletion in production. A REMOVED one leaves a
    stale entry, which fails too: the registry entry is the record of a decision, and a
    decision about a constraint that no longer exists is misleading documentation plus,
    usually, a dead code branch.
    """
    live = _live_non_cascade_fks(db_session)

    unregistered = sorted(set(live) - set(REGISTRY))
    stale = sorted(set(REGISTRY) - set(live))
    assert (unregistered, stale) == ([], []), (
        f"unregistered non-CASCADE FKs into user/media_file: {unregistered}\n"
        f"registered but no longer in the schema: {stale}\n"
        "Every one needs a Disposition saying which deletion path clears it (or that "
        "the DB rule is the handler) and why."
    )


def test_the_derivation_finds_the_foreign_keys_it_claims_to(db_session):
    """Guard the guard: a query matching nothing would pass every test above.

    Three known facts. ``media_file.user_id`` must be found (the FK the whole owner
    cascade exists for) and ``transcript_segment.speaker_id`` must be found (proof the
    third parent table is really in the query, which is the one that had a live break);
    ``file_tag.media_file_id`` must NOT be, because it is ``ON DELETE CASCADE`` — exactly
    what the filter excludes.
    """
    live = _live_non_cascade_fks(db_session)

    assert "media_file.user_id" in live
    assert "transcript_segment.speaker_id" in live
    assert "file_tag.media_file_id" not in live
    assert len(live) > 10, f"only {len(live)} FKs found; the query has stopped matching"


def test_no_other_deletion_parent_has_grown_a_non_cascade_child(db_session):
    """The parent set is three tables because the others are fully CASCADE. Verify that.

    ``tag``, ``collection``, ``speaker_collection``, ``speaker_profile`` and
    ``speaker_cluster`` are all deleted by one path or the other, and every FK into them
    is ``ON DELETE CASCADE`` or ``SET NULL`` — which is why they need no registry entries.
    That is a fact about today's schema, not a property of those tables: the moment a
    revision adds a ``NO ACTION`` child to one of them, that table has to join
    ``_PARENT_TABLES`` and its child needs an entry. Without this test the omission would
    be invisible, which is exactly how ``speaker`` was missed.
    """
    others = ("tag", "collection", "speaker_collection", "speaker_profile", "speaker_cluster")
    live = _live_non_cascade_fks(db_session, parents=others)

    no_action = {key: rule for key, rule in live.items() if rule != SET_NULL}
    assert no_action == {}, (
        f"these FKs into {others} are neither CASCADE nor SET NULL: {no_action}. Add the "
        "parent to _PARENT_TABLES and register the child, or the deletion paths will "
        "start failing on it."
    )


def test_the_declared_delete_rules_match_the_live_schema(db_session):
    """The registry's ``rule`` must match the database, for every entry.

    This is what fails on a database that has not run ``v387`` yet — the five actor FKs
    declared ``SET NULL`` here are still ``NO ACTION`` there — and it is also what fails
    if a later migration reverts one, which would restore the 500 with no other symptom.

    One test rather than 25 parametrised ones so a stale database reports every
    mismatch in a single run instead of one per fix.
    """
    live = _live_non_cascade_fks(db_session)
    mismatched = {
        key: (live[key], entry.rule)
        for key, entry in REGISTRY.items()
        if key in live and live[key] != entry.rule
    }
    assert mismatched == {}, (
        f"delete-rule mismatches (fk: schema_confdeltype, registry_confdeltype): "
        f"{mismatched}. 'a' = NO ACTION, 'n' = SET NULL. If the schema is right, update "
        "the entry; if the registry is right, this database is behind on migrations."
    )


@pytest.mark.parametrize("fk_key", sorted(REGISTRY))
def test_every_registry_entry_carries_a_written_reason(fk_key):
    """A reason is mandatory, as in ``test_ddl_marker_discipline``'s allowlist.

    An entry without one is indistinguishable from a to-do, and this registry is the
    only place the shape of the deletion paths is written down.
    """
    reason = REGISTRY[fk_key].reason
    assert len(reason.split()) >= 6, f"{fk_key} needs a real reason, got {reason!r}"
    placeholders = [
        word for word in ("TODO", "FIXME", "XXX", "tbd", "n/a") if word.lower() in reason.lower()
    ]
    assert placeholders == [], f"{fk_key}'s reason is a placeholder: {reason!r}"


@pytest.mark.parametrize("fk_key", sorted(REGISTRY))
def test_a_registered_handler_still_mentions_its_model(fk_key):
    """The link between a registry claim and the code, so deleting the branch fails here.

    A dotted handler is only evidence if it still deletes the thing — an entry naming
    ``_delete_user_media_files`` for ``Comment`` stops being true the moment that
    branch is removed, and nothing else in the suite would notice until the endpoint
    500s in production. Sentinel values name no code and are checked by the three
    per-sentinel tests below instead.
    """
    entry = REGISTRY[fk_key]
    named = [p for p in (entry.admin_path, entry.gdpr_path) if p not in _SENTINELS]

    unmentioned = [path for path in named if entry.model not in _handler_source(path)]
    assert unmentioned == [], (
        f"{fk_key}: {unmentioned} no longer mention {entry.model}. Either the deletion "
        "branch was removed (the FK is now unhandled) or the registry is out of date."
    )


#: One key list per sentinel, computed once. Three narrow parametrisations rather than one
#: test with three ``if`` blocks: an assertion inside an ``if`` passes whenever the
#: condition is false, so the single-test version reported 25 passes while really checking
#: whichever subset happened to match (scripts/audit-tests.py flags exactly that shape).
_SET_NULL_CLAIMS = sorted(
    key for key, e in REGISTRY.items() if DB_SET_NULL in (e.admin_path, e.gdpr_path)
)
_SIBLING_CASCADE_CLAIMS = sorted(
    key for key, e in REGISTRY.items() if DB_CASCADE_VIA_SIBLING in (e.admin_path, e.gdpr_path)
)
_ORM_CASCADE_CLAIMS = sorted(
    key for key, e in REGISTRY.items() if ORM_CASCADE in (e.admin_path, e.gdpr_path)
)

#: For a ``DB_CASCADE_VIA_SIBLING`` claim, WHICH sibling column does the work. Naming it
#: rather than counting CASCADE FKs on the table matters: a table can gain an unrelated
#: CASCADE FK and lose the load-bearing one, and a count would not move.
_SIBLING_CASCADE_COLUMN = {"speaker.user_id": "media_file_id"}


def test_every_sentinel_is_exercised_by_a_real_parametrisation():
    """Guard the guard: ``parametrize`` over an EMPTY list collects zero tests.

    Zero collected tests is indistinguishable from zero failures in a summary line, so
    each of the three lists has to be non-empty, and together they must account for every
    entry that claims a sentinel at all — otherwise a fourth sentinel could be introduced
    and checked by nothing.
    """
    assert _SET_NULL_CLAIMS, "no entry claims DB_SET_NULL; the check below runs on nothing"
    assert _SIBLING_CASCADE_CLAIMS, "no entry claims DB_CASCADE_VIA_SIBLING"
    assert _ORM_CASCADE_CLAIMS, "no entry claims ORM_CASCADE"

    covered = set(_SET_NULL_CLAIMS) | set(_SIBLING_CASCADE_CLAIMS) | set(_ORM_CASCADE_CLAIMS)
    claiming = {key for key, e in REGISTRY.items() if _SENTINELS & {e.admin_path, e.gdpr_path}}
    assert covered == claiming, f"sentinel claims checked by nothing: {sorted(claiming - covered)}"
    assert set(_SIBLING_CASCADE_COLUMN) == set(_SIBLING_CASCADE_CLAIMS), (
        "every DB_CASCADE_VIA_SIBLING claim must name the sibling column that actually "
        f"removes the row: {sorted(set(_SIBLING_CASCADE_CLAIMS) - set(_SIBLING_CASCADE_COLUMN))}"
    )


@pytest.mark.parametrize("fk_key", _SET_NULL_CLAIMS)
def test_a_db_set_null_claim_matches_the_schema(db_session, fk_key):
    """ "The database blanks it" — verified against ``pg_constraint``, not trusted.

    These entries name no code at all, so the FK's own rule is the entire handler. If a
    migration reverted one to ``NO ACTION`` the owning account would become undeletable
    again, with a 500 that names nothing, and no other test in the tree would notice.
    """
    assert _live_non_cascade_fks(db_session)[fk_key] == SET_NULL, (
        f"{fk_key} claims the DB's SET NULL rule is its handler, but the schema says "
        "otherwise — either the migration has not run here, or it was reverted"
    )


@pytest.mark.parametrize("fk_key", _SIBLING_CASCADE_CLAIMS)
def test_a_sibling_cascade_claim_still_has_its_sibling(db_session, fk_key):
    """ "A different FK on this table cascades" — so the row is gone before this one counts.

    ``speaker.user_id`` is the only such case: the GDPR path never queries speakers by
    owner, it deletes files as instances and ``speaker.media_file_id``'s real CASCADE
    takes them. Remove that CASCADE and the claim becomes false silently.

    The assertion is membership of the NAMED column in the table's cascading set, not a
    count — a table can gain an unrelated CASCADE FK while losing the load-bearing one,
    and a count would not move.
    """
    table = fk_key.split(".")[0]
    cascading_columns = set(
        db_session.connection()
        .execute(
            text(
                "SELECT a.attname FROM pg_constraint c "
                "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
                "WHERE c.conrelid = CAST(:tbl AS regclass) AND c.contype = 'f' "
                "  AND c.confdeltype = 'c'"
            ),
            {"tbl": table},
        )
        .scalars()
        .all()
    )
    expected = _SIBLING_CASCADE_COLUMN[fk_key]
    assert expected in cascading_columns, (
        f"{fk_key} relies on {table}.{expected} being ON DELETE CASCADE; the cascading "
        f"columns on {table} are now {sorted(cascading_columns)}"
    )


@pytest.mark.parametrize("fk_key", _ORM_CASCADE_CLAIMS)
def test_an_orm_cascade_claim_is_still_declared(fk_key):
    """ "``MediaFile``'s delete-orphan sweeps it" — verified against the mapper.

    The ``cascade=`` keyword is one edit away from being dropped, and these four FKs are
    ``NO ACTION``, so dropping it does not orphan rows: it makes ``purge_media_file``
    start returning ``deleted: False`` for every file, which ``erase_user`` records as an
    error in its summary rather than raising.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models.media import MediaFile

    entry = REGISTRY[fk_key]
    cascades = {
        rel.mapper.class_.__name__: rel.cascade for rel in sa_inspect(MediaFile).relationships
    }
    assert "delete-orphan" in cascades.get(entry.model, ()), (
        f"{fk_key} relies on MediaFile's delete-orphan cascade to {entry.model}, which is "
        f"no longer declared (found {cascades.get(entry.model)})"
    )


@pytest.mark.parametrize("fk_key", sorted(_OWNER_SCOPED))
def test_an_owner_scoped_fk_is_claimed_by_both_paths(fk_key):
    """Neither path may leave an owner-scoped FK unclaimed.

    ``erase_user`` and ``DELETE /admin/users/{uuid}`` are separate implementations of
    the same cascade and each has failed at a different foreign key. An owner-scoped FK
    must be claimed by both — by a named helper, or by a sentinel whose mechanism the
    reason spells out.
    """
    entry = _OWNER_SCOPED[fk_key]
    for label, path in (("admin", entry.admin_path), ("gdpr", entry.gdpr_path)):
        assert path, f"{fk_key}: the {label} path claims nothing"
        assert path in _SENTINELS or path.startswith("app."), (
            f"{fk_key}: the {label} path claims {path!r}, which is neither a dotted "
            "handler nor one of the checked sentinels"
        )


def test_the_two_hand_maintained_owner_lists_agree():
    """Compare the two lists nothing in the application compares.

    ``admin._delete_user_owned_records`` and
    ``gdpr_erasure_service._delete_owner_scoped_rows`` were written independently and
    have drifted: each deletes models the other does not. Divergence is legitimate —
    the two use different delete shapes, and only one has a legal-hold branch — but it
    has to be a *decision*, and the four current ones are recorded in
    ``_REGISTERED_LIST_DIVERGENCES`` with the mechanism that makes each safe.
    """
    admin_models = _referenced_model_names(_ADMIN_OWNED)
    gdpr_models = _referenced_model_names(_GDPR_ROWS)

    divergent = admin_models ^ gdpr_models
    unexplained = sorted(divergent - set(_REGISTERED_LIST_DIVERGENCES))
    obsolete = sorted(set(_REGISTERED_LIST_DIVERGENCES) - divergent)

    assert (unexplained, obsolete) == ([], []), (
        f"models deleted by one owner-scoped path and not the other, with no reason "
        f"on record: {unexplained}\n"
        f"reasons recorded for divergences that no longer exist: {obsolete}"
    )


def test_the_two_hand_maintained_owner_lists_overlap_at_all():
    """Guard the guard for the comparison above.

    ``_referenced_model_names`` resolves local names through the defining module's
    globals (``admin.py`` imports ``Task as TaskModel``). If that resolution broke, both
    sets would come back empty or disjoint and the symmetric difference would read as
    either "no divergence" or "everything diverges" — both indistinguishable from a real
    result.
    """
    admin_models = _referenced_model_names(_ADMIN_OWNED)
    gdpr_models = _referenced_model_names(_GDPR_ROWS)

    assert {"SpeakerProfile", "Collection", "Tag", "Comment", "Task"} <= admin_models
    assert {"SpeakerProfile", "Collection", "Tag", "Comment", "Task"} <= gdpr_models
