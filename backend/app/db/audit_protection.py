"""Registry of audit tables the application must not be able to rewrite or delete.

One source of truth for three consumers: the immutability trigger migration (a frozen copy of
``trigger_arguments`` lives in the revision), the app-role grant reconcile in ``roles.py``, and
the status endpoint. A table is either protected here or listed in ``EXEMPT_TABLES`` with the
reason; ``tests/unit/test_audit_protection_registry.py`` fails on an audit-shaped table that
is neither.

Never widen ``mutable_columns`` without a failing test that needs the column.
"""

from dataclasses import dataclass

GUARD_FUNCTION = "audit_row_guard"
PRUNE_FUNCTION = "audit_prune"


def guard_trigger_name(table: str) -> str:
    """Name of the generic immutability trigger on ``table``."""
    return f"trg_audit_guard_{table}"


@dataclass(frozen=True)
class ProtectedTable:
    """One protected table.

    Attributes:
        name: Table name in the ``public`` schema.
        mutable_columns: Columns the application may UPDATE (lifecycle state). ``()`` means
            the table is insert-only.
        nullable_on_cascade: FK columns an ``ON DELETE SET NULL`` may blank, so deleting a user
            or organization still works while the audit row survives.
        reason: Why it is protected.
        own_guard: Name of a table-specific guard trigger that replaces the generic one
            (``legal_acceptance``: it pseudonymises on erasure and allows its own retention
            sweep). The grants below still apply.
        app_delete: The application role keeps DELETE on the table. Only meaningful with
            ``own_guard``: the guard trigger then decides which deletes are allowed.
        prune_column: Timestamp column the retention sweep ages rows by; ``None`` means the
            table is never pruned by ``audit_prune``.
    """

    name: str
    mutable_columns: tuple[str, ...] = ()
    nullable_on_cascade: tuple[str, ...] = ()
    reason: str = ""
    own_guard: str = ""
    app_delete: bool = False
    prune_column: str | None = None


PROTECTED_TABLES: tuple[ProtectedTable, ...] = (
    ProtectedTable(
        name="auth_config_audit",
        nullable_on_cascade=("changed_by",),
        reason="authentication configuration change log",
        prune_column="created_at",
    ),
    ProtectedTable(
        name="support_access_use",
        reason="per-request record of support-access use",
        prune_column="occurred_at",
    ),
    ProtectedTable(
        name="support_access_grant",
        mutable_columns=(
            "decided_by_user_id",
            "decided_at",
            "decision",
            "starts_at",
            "expires_at",
            "revoked_at",
            "revoked_by_user_id",
        ),
        nullable_on_cascade=(
            "organization_id",
            "subject_user_id",
            "grantee_user_id",
            "decided_by_user_id",
            "revoked_by_user_id",
        ),
        reason="support-access grant lifecycle (the parent of support_access_use)",
    ),
    ProtectedTable(
        name="erasure_ledger",
        mutable_columns=(
            "status",
            "completed_at",
            "last_attempt_at",
            "attempts",
            "deferred_reason",
            "legal_holds_outstanding",
            "error_count",
            "counters",
            "resurrections_detected",
            "last_resurrection_at",
        ),
        nullable_on_cascade=("actor_user_id",),
        reason="Art. 17/30 erasure ledger (evidence; never pruned)",
    ),
    ProtectedTable(
        name="takedown_notice",
        mutable_columns=("updated_at",),
        nullable_on_cascade=("handled_by_user_id",),
        reason="legal notice record (never pruned)",
    ),
    ProtectedTable(
        name="takedown_notice_file",
        mutable_columns=(
            "status",
            "counter_notice_received_at",
            "outcome_at",
            "outcome_note",
            "legal_hold_placed",
        ),
        nullable_on_cascade=("file_id", "organization_id"),
        reason="files covered by a legal notice (never pruned)",
    ),
    ProtectedTable(
        name="takedown_notification",
        mutable_columns=("status", "attempted_at", "recipient_user_id"),
        nullable_on_cascade=("recipient_user_id",),
        reason="takedown notification delivery log (never pruned)",
    ),
    ProtectedTable(
        name="legal_acceptance",
        mutable_columns=(
            "user_id",
            "ip_address",
            "user_agent",
            "subject_erased_at",
            "retain_until",
            "organization_id",
        ),
        nullable_on_cascade=("user_id", "organization_id"),
        reason="evidence of which legal text an account or organization accepted",
        own_guard="trg_legal_acceptance_guard",
        app_delete=True,
    ),
)

#: Audit-shaped tables that are deliberately not protected, each with the reason.
EXEMPT_TABLES: dict[str, str] = {
    "content_hold": (
        "tenant data that goes with its organization on erasure (organization_id is CASCADE); "
        "placement and release are already in the OpenSearch audit stream"
    ),
    "content_hold_file": "child of content_hold, same reason",
    "usage_event": (
        "cloud-edition seam table: a downstream edition owns its lifecycle and may opt in "
        "through register_protected_table"
    ),
}

_extra: list[ProtectedTable] = []


def register_protected_table(table: ProtectedTable) -> None:
    """Edition hook: protect an additional table. Call before the startup reconcile."""
    if any(t.name == table.name for t in all_protected_tables()):
        raise ValueError(f"{table.name} is already registered as protected")
    _extra.append(table)


def all_protected_tables() -> tuple[ProtectedTable, ...]:
    """Built-in plus edition-registered protected tables."""
    return PROTECTED_TABLES + tuple(_extra)


def generic_guard_tables() -> tuple[ProtectedTable, ...]:
    """Tables that carry the generic ``audit_row_guard`` trigger."""
    return tuple(t for t in all_protected_tables() if not t.own_guard)


def trigger_arguments(table: ProtectedTable) -> tuple[str, str]:
    """The two text[] literals passed to ``audit_row_guard`` for ``table``."""
    return (
        "{" + ",".join(table.mutable_columns) + "}",
        "{" + ",".join(table.nullable_on_cascade) + "}",
    )


def prunable_tables() -> tuple[ProtectedTable, ...]:
    """Tables ``audit_prune`` may age out (a hardcoded subset, see the migration)."""
    return tuple(t for t in PROTECTED_TABLES if t.prune_column)
