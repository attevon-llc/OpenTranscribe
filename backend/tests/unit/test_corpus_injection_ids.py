"""Determinism and idempotency of injected-corpus identifiers (issue #403).

Re-running the injector must not duplicate a corpus, and a ``file_uuid`` recorded
in a results table months from now must still resolve to the meeting that
produced it. Both properties reduce to: the identifier is a pure function of
``(corpus, seed, meeting_id)``. These tests pin that function's *values*, not
just its self-consistency — a test that only compared the function to itself
would still pass after someone changed the namespace and silently renumbered
every corpus ever injected.
"""

from __future__ import annotations

import uuid

from app.scripts.corpus_injection import ids
from app.scripts.corpus_injection.model import Turn


def _turns(*pairs: tuple[str, str]) -> list[Turn]:
    return [Turn(turn_index=i, speaker=s, text=t) for i, (s, t) in enumerate(pairs)]


class TestFileUuidDeterminism:
    def test_same_inputs_give_same_uuid_across_calls(self):
        first = ids.file_uuid("qmsum", "ES2004a")
        second = ids.file_uuid("qmsum", "ES2004a")
        assert first == second

    def test_uuid_is_pinned_not_merely_self_consistent(self):
        """Freeze the actual value.

        Without this, changing ``CORPUS_NAMESPACE`` or the key separator would
        keep every other test in this file green while orphaning every row a
        previous run wrote.
        """
        assert str(ids.file_uuid("qmsum", "ES2004a")) == "b7457359-923c-5252-b7af-2dc4349de4ce"
        assert str(ids.file_uuid("qmsum", "covid_0")) == "b0ee6fe2-7c3d-53e2-a2bc-2bbf0ec73960"

    def test_different_meetings_differ(self):
        assert ids.file_uuid("qmsum", "ES2004a") != ids.file_uuid("qmsum", "ES2004b")

    def test_different_corpora_differ_for_the_same_meeting_id(self):
        assert ids.file_uuid("qmsum", "ES2004a") != ids.file_uuid("ami", "ES2004a")

    def test_seed_creates_a_disjoint_namespace(self):
        canonical = ids.file_uuid("qmsum", "ES2004a")
        seeded = ids.file_uuid("qmsum", "ES2004a", seed="pytest-1")
        assert canonical != seeded
        assert seeded == ids.file_uuid("qmsum", "ES2004a", seed="pytest-1")

    def test_seed_and_meeting_id_cannot_be_confused(self):
        """A separator that leaked would make ('a','bc') and ('ab','c') collide."""
        assert ids.file_uuid("qmsum", "bc", seed="a") != ids.file_uuid("qmsum", "c", seed="ab")

    def test_result_is_a_valid_uuid_object(self):
        value = ids.file_uuid("qmsum", "ES2004a")
        assert isinstance(value, uuid.UUID)
        assert value.version == 5


class TestSegmentAndSpeakerUuids:
    def test_segment_uuids_are_stable_per_position(self):
        assert ids.segment_uuid("qmsum", "M1", "", 7) == ids.segment_uuid("qmsum", "M1", "", 7)
        assert ids.segment_uuid("qmsum", "M1", "", 7) != ids.segment_uuid("qmsum", "M1", "", 8)

    def test_segment_and_file_uuids_never_collide(self):
        """Distinct ``kind`` prefixes are what keep the three families apart."""
        assert ids.segment_uuid("qmsum", "M1", "", 0) != ids.file_uuid("qmsum", "M1")

    def test_speaker_uuid_keyed_on_label(self):
        alice = ids.speaker_uuid("qmsum", "M1", "", "Project Manager")
        bob = ids.speaker_uuid("qmsum", "M1", "", "Marketing")
        assert alice != bob
        assert alice == ids.speaker_uuid("qmsum", "M1", "", "Project Manager")

    def test_speaker_uuid_is_scoped_to_the_meeting(self):
        assert ids.speaker_uuid("qmsum", "M1", "", "PM") != ids.speaker_uuid(
            "qmsum", "M2", "", "PM"
        )


class TestContentFingerprint:
    def test_identical_transcripts_hash_identically(self):
        a = _turns(("A", "hello there"), ("B", "hi"))
        b = _turns(("A", "hello there"), ("B", "hi"))
        assert ids.content_sha256(a) == ids.content_sha256(b)

    def test_changed_text_changes_the_hash(self):
        base = _turns(("A", "hello there"), ("B", "hi"))
        edited = _turns(("A", "hello there"), ("B", "hey"))
        assert ids.content_sha256(base) != ids.content_sha256(edited)

    def test_changed_speaker_changes_the_hash(self):
        base = _turns(("A", "hello"), ("B", "hi"))
        reattributed = _turns(("A", "hello"), ("C", "hi"))
        assert ids.content_sha256(base) != ids.content_sha256(reattributed)

    def test_reordered_turns_change_the_hash(self):
        assert ids.content_sha256(_turns(("A", "x"), ("B", "y"))) != ids.content_sha256(
            _turns(("B", "y"), ("A", "x"))
        )

    def test_field_boundaries_are_not_ambiguous(self):
        """Concatenating without separators would make these two collide."""
        assert ids.content_sha256(_turns(("AB", "C"))) != ids.content_sha256(_turns(("A", "BC")))

    def test_timings_are_excluded_from_the_fingerprint(self):
        """Same text, different timing provenance, same content — by design.

        The hash answers "did the corpus text change?", which is what the
        idempotency check needs. Folding timings in would make every meeting
        look changed the first time real timings replaced synthetic ones.
        """
        untimed = _turns(("A", "hello"))
        timed = _turns(("A", "hello"))
        timed[0].start, timed[0].end = 1.0, 2.0
        assert ids.content_sha256(untimed) == ids.content_sha256(timed)
