"""No ORM relationship may pre-empt a database ``ON DELETE CASCADE`` by nulling the FK.

The defect this exists to catch
-------------------------------
``SpeakerCluster.user`` was declared ``relationship("User", backref="speaker_clusters")``.
``speaker_cluster.user_id`` is ``NOT NULL`` and ``ON DELETE CASCADE`` in the schema, so the
database would have removed the clusters with the account by itself. But a plain backref
tells SQLAlchemy that, on ``db.delete(user)``, it owns the children: it loads them and
issues ``UPDATE speaker_cluster SET user_id=NULL`` *before* the ``DELETE FROM "user"`` —
which the ``NOT NULL`` refuses. Every account with a single speaker cluster (clustering
runs after every transcription) therefore could not be deleted:
``DELETE /api/admin/users/{uuid}`` answered ``500 "User deletion failed"`` and
``DELETE /api/users/{uuid}`` raised. Found by ``integration/test_deletion_residue_live.py``.

The rule is structural, so it is checked structurally, across EVERY mapper: a one-to-many
relationship whose foreign key is ``NOT NULL`` and ``ON DELETE CASCADE`` must either cascade
the delete itself or declare ``passive_deletes=True`` (leave it to the database). Anything
else turns the database's cascade into a constraint violation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import configure_mappers
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.orm.interfaces import ONETOMANY


def _preempting_relationships(mappers) -> list[str]:
    """``Class.rel`` for every relationship that would NULL a NOT NULL, CASCADE foreign key."""
    found = []
    for mapper in mappers:
        for rel in mapper.relationships:
            if rel.direction is not ONETOMANY or rel.viewonly:
                continue
            cols = list(rel.remote_side)
            not_null = bool(cols) and all(not col.nullable for col in cols)
            rules = {
                (fk.ondelete or "NO ACTION").upper() for col in cols for fk in col.foreign_keys
            }
            handled = "delete" in rel.cascade or rel.passive_deletes
            if not_null and rules == {"CASCADE"} and not handled:
                found.append(f"{mapper.class_.__name__}.{rel.key}")
    return sorted(found)


@pytest.mark.unit
def test_no_relationship_nulls_a_cascading_not_null_foreign_key() -> None:
    import app.models  # noqa: F401 — registers every mapper
    from app.db.base import Base

    configure_mappers()
    mappers = list(Base.registry.mappers)
    assert len(mappers) > 50, f"expected the full model registry, got {len(mappers)} mappers"
    assert _preempting_relationships(mappers) == []


@pytest.mark.unit
def test_the_detector_fires_on_a_preempting_relationship_and_not_on_a_passive_one() -> None:
    """Guard the guard: a scan that matches nothing would pass the real registry too."""

    class _Base(DeclarativeBase):
        pass

    class Owner(_Base):
        __tablename__ = "owner"
        id: Mapped[int] = mapped_column(primary_key=True)
        bare: Mapped[list[Bare]] = relationship()
        passive: Mapped[list[Passive]] = relationship(passive_deletes=True)
        cascading: Mapped[list[Cascading]] = relationship(cascade="all, delete-orphan")

    class Bare(_Base):
        __tablename__ = "bare"
        id: Mapped[int] = mapped_column(primary_key=True)
        owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"))

    class Passive(_Base):
        __tablename__ = "passive"
        id: Mapped[int] = mapped_column(primary_key=True)
        owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"))

    class Cascading(_Base):
        __tablename__ = "cascading"
        id: Mapped[int] = mapped_column(primary_key=True)
        owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"))

    class Nullable(_Base):
        __tablename__ = "nullable_child"
        id: Mapped[int] = mapped_column(primary_key=True)
        owner_id: Mapped[int | None] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"))
        owner: Mapped[Owner] = relationship(backref="nullable_children")

    _Base.registry.configure()
    assert _preempting_relationships(list(_Base.registry.mappers)) == ["Owner.bare"]
