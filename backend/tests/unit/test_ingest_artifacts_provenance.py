"""Digest provenance is a tagged union with BOTH arms live (#403 D3).

The second arm (``char_range``) has no producer until #362 lands, which is exactly why it
is tested now: an untested variant is a variant that will not work when Stage 6 reaches
for it, and by then the shape is baked into stored JSONB.
"""

from __future__ import annotations

import pytest

from app.services.ingest_artifacts.provenance import KIND_CHAR_RANGE
from app.services.ingest_artifacts.provenance import KIND_SEGMENT_IDS
from app.services.ingest_artifacts.provenance import PROVENANCE_KINDS
from app.services.ingest_artifacts.provenance import ProvenanceError
from app.services.ingest_artifacts.provenance import char_range_provenance
from app.services.ingest_artifacts.provenance import provenance_timespan
from app.services.ingest_artifacts.provenance import segment_provenance
from app.services.ingest_artifacts.provenance import validate_provenance


def test_the_kind_constants_equal_the_literals_the_builders_write():
    """mypy forced the builders to inline the literal instead of using the constant.

    That is two spellings of one value, and this is the assertion that keeps them equal —
    otherwise a rename of the constant leaves readers filtering on a string nothing
    writes, which is a silent empty result rather than an error.
    """
    assert segment_provenance([1], 0.0, 1.0)["kind"] == KIND_SEGMENT_IDS
    assert char_range_provenance(0, 10)["kind"] == KIND_CHAR_RANGE
    assert set(PROVENANCE_KINDS) == {KIND_SEGMENT_IDS, KIND_CHAR_RANGE}


def test_segment_ids_are_stored_sorted_and_deduplicated():
    """A set→list of ids would vary per worker process; the digest must not."""
    payload = segment_provenance([9, 3, 3, 7], 12.0, 20.0)
    assert payload["segment_ids"] == [3, 7, 9]


def test_the_transcript_arm_carries_real_timestamps_not_zero():
    """Addendum G7: a digest citation with ``start_time=0`` deep-links to 0:00."""
    payload = segment_provenance([4], 137.456, 149.2)
    assert payload["start_time"] == pytest.approx(137.46)
    assert payload["end_time"] == pytest.approx(149.2)
    assert provenance_timespan(payload) == (137.46, 149.2)


def test_the_document_arm_has_no_timespan_so_stage_4_must_not_fake_one():
    """``None`` is the signal for a file-level citation, not a 0:00 deep link."""
    assert provenance_timespan(char_range_provenance(100, 240, page=4)) is None


def test_page_is_omitted_rather_than_null_when_the_parser_has_none():
    assert "page" not in char_range_provenance(0, 10)
    assert char_range_provenance(0, 10, page=2)["page"] == 2


def test_a_backwards_char_range_is_refused_at_construction():
    with pytest.raises(ProvenanceError):
        char_range_provenance(500, 100)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("not a dict", "non-dict"),
        ({"segment_ids": [1], "start_time": 0.0, "end_time": 1.0}, "missing discriminator"),
        ({"kind": "page_number", "page": 3}, "unknown kind"),
        ({"kind": "segment_ids", "start_time": 0.0, "end_time": 1.0}, "no ids"),
        ({"kind": "segment_ids", "segment_ids": [], "start_time": 0.0, "end_time": 1.0}, "empty"),
        (
            {"kind": "segment_ids", "segment_ids": [2, 1], "start_time": 0.0, "end_time": 1.0},
            "unsorted",
        ),
        ({"kind": "segment_ids", "segment_ids": [1], "end_time": 1.0}, "no start_time"),
        ({"kind": "char_range", "char_start": 0}, "no char_end"),
        ({"kind": "char_range", "char_start": 10, "char_end": 1}, "reversed"),
    ],
)
def test_validate_refuses_every_malformed_shape(payload, reason):
    with pytest.raises(ProvenanceError):
        validate_provenance(payload)
    assert reason  # the parametrisation label is the documentation


def test_validate_accepts_both_well_formed_arms():
    """The validator signals by RAISING, so acceptance is "no exception".

    Paired with its own control below, because a bare "it did not raise" is
    indistinguishable both from a test that forgot to assert and from a validator that
    never rejects anything.
    """
    good_segment = segment_provenance([1, 2], 0.0, 5.0)
    good_char = char_range_provenance(0, 100, page=1)
    validate_provenance(good_segment)
    validate_provenance(good_char)

    # The control. Without it, "it did not raise" would also be satisfied by a validator
    # that raises for nothing at all — which is what the function would become if the
    # discriminator check were dropped.
    with pytest.raises(ProvenanceError):
        validate_provenance({k: v for k, v in good_segment.items() if k != "kind"})
    with pytest.raises(ProvenanceError):
        validate_provenance({k: v for k, v in good_char.items() if k != "kind"})
