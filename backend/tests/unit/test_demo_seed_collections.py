"""The demo seed files each item in its named collection, owned by the demo user (#628)."""

from __future__ import annotations

from pathlib import Path

from fixtures.speaker_cleanup_seams import make_file
from sqlalchemy import select

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.scripts.demo_seed.__main__ import _file_into_collections
from app.scripts.demo_seed.bundle import SeedItem


def _item(item_id: str, collection: str) -> SeedItem:
    return SeedItem(
        id=item_id,
        title=item_id,
        language="en",
        media=Path("m.mp3"),
        media_sha256="0",
        transcript=Path("t.json"),
        collection=collection,
        license="US-GOV-PD",
        source_url="https://example.gov",
        license_url="https://example.gov/l",
        attribution="x",
        synthetic_pii=False,
    )


def test_items_land_in_their_named_collections_owned_by_the_demo_user(db_session, normal_user):
    first, second, third = (make_file(db_session, normal_user) for _ in range(3))
    db_session.commit()
    items = [_item("a", "news"), _item("b", "news"), _item("c", "privacy-demo")]
    uuids = {"a": str(first.uuid), "b": str(second.uuid), "c": str(third.uuid)}

    _file_into_collections(db_session, normal_user.id, items, uuids)
    db_session.commit()

    rows = list(db_session.scalars(select(Collection).where(Collection.user_id == normal_user.id)))
    assert sorted(r.name for r in rows) == ["News", "Privacy demo"]
    members = {r.name: sorted(m.media_file_id for m in r.collection_members) for r in rows}
    assert members == {
        "News": sorted([first.id, second.id]),
        "Privacy demo": [third.id],
    }
    assert db_session.scalar(select(CollectionMember.id).limit(1)) is not None
