"""``org_stamp_is``: NULL-safe tenant-stamp predicate, one copy in ``utils/db_helpers``.

``utils/file_hash.py`` carried an untyped duplicate; both now use the typed one.
"""

from sqlalchemy.dialects import postgresql

from app.models.media import MediaFile
from app.utils.db_helpers import org_stamp_is


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_none_matches_org_less_rows_only():
    assert _sql(org_stamp_is(MediaFile.organization_id, None)).endswith("organization_id IS NULL")


def test_an_org_id_matches_that_orgs_rows_only():
    assert _sql(org_stamp_is(MediaFile.organization_id, 7)).endswith("organization_id = 7")


def test_org_id_zero_is_an_org_not_personal_scope():
    # 0 is falsy: a truthiness check in place of `is None` would send it to IS NULL.
    assert _sql(org_stamp_is(MediaFile.organization_id, 0)).endswith("organization_id = 0")


def test_file_hash_no_longer_defines_its_own_copy():
    from app.utils import file_hash

    assert file_hash.org_stamp_is is org_stamp_is
