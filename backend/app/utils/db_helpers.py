import logging
from typing import Any

from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy.orm import QueryableAttribute
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import TranscriptSegment

logger = logging.getLogger(__name__)


def org_stamp_is(
    org_col: QueryableAttribute[Any] | ColumnElement[Any], organization_id: int | None
) -> ColumnElement[bool]:
    """NULL-safe predicate: a row's ``organization_id`` stamp names the active tenant.

    An org id matches that org's rows only; ``None`` (personal scope) matches org-less
    rows only. Community-edition invariance: every row is org-less and every caller is
    in personal scope, so this is always true there.
    """
    if organization_id is None:
        return org_col.is_(None)
    return org_col == organization_id


def owned_in_tenant(
    model: Any, *, user_id: int, organization_id: OrgScope = UNSCOPED
) -> ColumnElement[bool]:
    """Owner-listing predicate: the caller's OWN rows, restricted to the active tenant.

    Unlike ``apply_tenant_scope`` this never widens an org-context listing to other
    members' rows — it keeps ``user_id == caller`` and only adds the tenant stamp.
    ``UNSCOPED`` (no context threaded) is plain ``user_id`` filtering.
    """
    owner: ColumnElement[bool] = model.user_id == user_id
    if isinstance(organization_id, _Unscoped):
        return owner
    return and_(owner, org_stamp_is(model.organization_id, organization_id))


def file_has_transcript(db: Session, file_id: int) -> bool:
    """Whether the file has at least one transcript segment, as one ``EXISTS``.

    Not ``bool(media_file.transcript_segments)``: that lazy-loads every column of every
    segment to answer a yes/no question (concurrency audit L7).
    """
    return bool(db.query(exists().where(TranscriptSegment.media_file_id == file_id)).scalar())
