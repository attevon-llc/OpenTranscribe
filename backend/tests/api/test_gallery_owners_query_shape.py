"""``GET /files/owners`` picks distinct owner ids, then reads those users (#1337 G11).

The old statement joined every accessible file to ``user`` and de-duplicated the joined rows
before sorting; at 20k files that was the whole cost. The result must be unchanged: each owner
once, ordered by name, whatever the number of files they own.
"""

from __future__ import annotations

import re

from tests.query_budget import count_statements

OWNERS = "/api/files/owners"


def test_owners_come_from_a_semi_join_and_each_owner_appears_once(client, gov):
    for _ in range(4):
        gov.file(gov.alice)
    for _ in range(3):
        gov.file(gov.bob)
    shared = gov.collection(gov.bob)
    gov.put(shared, *[gov.file(gov.bob) for _ in range(2)])
    gov.share(shared, gov.alice, by=gov.bob)

    with gov.acting_as(gov.alice):
        with count_statements(gov.db.connection()) as log:
            response = client.get(OWNERS)

    assert response.status_code == 200, response.text
    uuids = [row["uuid"] for row in response.json()]
    assert sorted(uuids) == sorted({str(gov.alice.uuid), str(gov.bob.uuid)}), (
        "control: both owners, once each"
    )
    owner_reads = log.matching(r'FROM "user"')
    assert owner_reads, log.describe()
    assert [s for s in owner_reads if re.search(r'FROM "user"\s+JOIN media_file', s)] == []
    assert [s for s in owner_reads if "SELECT DISTINCT" in s.split("FROM")[0]] == []
