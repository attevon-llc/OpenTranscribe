"""Characterisation of the four UUID-parsing callers that share one parse core (K6).

``tasks._valid_uuids`` (filter), ``chat.language._parse_uuids`` (skip + log),
``speaker_clusters._require_uuid`` (404) and ``uuid_helpers.validate_uuids`` (400) each own a
deliberate FAILURE POLICY but parse identically. These pin the accepted and rejected
spellings of each, so replacing the four hand-written ``try/except UUID(...)`` blocks with one
core cannot change what any of them accepts.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import cast

import pytest
from fastapi import HTTPException

from app.api.endpoints.speaker_clusters import _require_uuid
from app.api.endpoints.tasks import _valid_uuids
from app.core.exceptions import InvalidInputError
from app.services.chat.language import _parse_uuids
from app.services.uuid_helpers import validate_uuids

_U = uuid_pkg.UUID("12345678-1234-5678-1234-567812345678")
_ACCEPTED = [
    str(_U),
    str(_U).upper(),
    "{" + str(_U) + "}",
    "urn:uuid:" + str(_U),
    _U.hex,
]
_REJECTED = ["", "not-a-uuid", " " + str(_U), str(_U)[:-1], "1234"]


@pytest.mark.parametrize("text", _ACCEPTED)
def test_every_caller_accepts_the_same_spellings(text):
    assert _valid_uuids({text}) == [str(_U)]
    assert _parse_uuids([text]) == [_U]
    _require_uuid(text, not_found_detail="nope")
    assert validate_uuids([text]) == [_U]


@pytest.mark.parametrize("text", _REJECTED)
def test_every_caller_rejects_the_same_spellings(text):
    assert _valid_uuids({text}) == []
    assert _parse_uuids([text]) == []
    with pytest.raises(HTTPException) as http_exc:
        _require_uuid(text, not_found_detail="nope")
    assert http_exc.value.status_code == 404
    assert http_exc.value.detail == "nope"
    with pytest.raises(InvalidInputError):
        validate_uuids([text])


def test_filtering_callers_keep_the_good_entries_beside_bad_ones():
    assert sorted(_valid_uuids({str(_U), "junk"})) == [str(_U)]
    assert _parse_uuids(cast(list[str], ["junk", str(_U), None, 5])) == [_U]


def test_validate_uuids_passes_uuid_objects_through():
    assert validate_uuids(cast(list[str], [_U])) == [_U]


def test_parse_uuid_or_none_core():
    from app.utils.uuid_parse import parse_uuid_or_none

    assert parse_uuid_or_none(_U) is _U
    assert parse_uuid_or_none(str(_U)) == _U
    assert parse_uuid_or_none(None) is None
    assert parse_uuid_or_none(5) is None
    assert parse_uuid_or_none(b"\x01") is None
    assert parse_uuid_or_none("junk") is None
