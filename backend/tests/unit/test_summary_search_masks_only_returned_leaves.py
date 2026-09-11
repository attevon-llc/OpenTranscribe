"""Issue #822: summary search must mask only the leaves it actually RETURNS,
not every leaf of every matching file.

Before this, `search_summaries` ran `mask_summary` (the whole-tree masker) once
per matching file and then discarded everything except the leaves that made it
into `matches` — so a detector outage on a leaf a caller was never going to see
still withheld the whole result with a 503. These tests prove the narrowing is
real: the number of `mask_summary_leaf` calls tracks the number of RETURNED
matches, not the number of leaves in the document, and the masked snippet is
identical to what the (still-correct) whole-tree masker would have produced
for that one leaf.
"""

from __future__ import annotations

import re
import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.media import MediaFile
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.summary_redaction import mask_summary
from app.services.search import summary_search
from app.services.search.summary_search import search_summaries

pytestmark = pytest.mark.unit


def _make_file(db_session, user, *, summary) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        user_id=user.id,
        status="completed",
        summary_data=summary,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _get_by_path(node: Any, path: str) -> Any:
    """Local, test-only re-implementation of the deleted production helper —
    deliberately duplicated here rather than imported, so this test proves the
    equivalence against an independent walk rather than against the same code
    the production path once shared.
    """
    current = node
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        current = current[int(part[1:-1])] if part.startswith("[") else current[part]
    return current


def _cfg() -> EffectiveRedactionConfig:
    return EffectiveRedactionConfig(
        enabled=True, enabled_categories={"custom"}, custom_words=["Zylofenix"]
    )


class TestOnlyReturnedLeavesAreMasked:
    def test_mask_summary_leaf_is_called_once_per_returned_match_not_per_leaf(
        self, db_session, normal_user, monkeypatch
    ):
        summary = {"bluf": "the Zylofenix roadmap review"}
        for i in range(20):
            summary[f"section_{i}"] = f"an unrelated passage number {i} about something else"

        _make_file(db_session, normal_user, summary=summary)

        calls: list[str] = []
        real_mask_summary_leaf = summary_search.mask_summary_leaf

        def _counting_spy(text, cfg):
            calls.append(text)
            return real_mask_summary_leaf(text, cfg)

        monkeypatch.setattr(summary_search, "mask_summary_leaf", _counting_spy)

        result = search_summaries(
            db_session, "roadmap", normal_user.id, organization_id=None, redaction_cfg=_cfg()
        )

        assert len(result.results) == 1
        matches = result.results[0].matches
        assert len(calls) == len(matches), (
            "mask_summary_leaf must be called exactly once per returned match, "
            "not once per leaf in the document"
        )
        # Non-emptiness asserted OUTSIDE the loop below: a zero-match run would
        # otherwise pass this vacuously (audit-tests' loop-only-assertion rule).
        assert matches, "expected at least one match for the count above to mean anything"
        for match in matches:
            assert match.snippet

    def test_the_masked_snippet_is_identical_to_the_whole_tree_masker(
        self, db_session, normal_user
    ):
        """The equivalence proof (issue #822): under the SAME config, masking
        just the returned leaf via `mask_summary_leaf` must produce exactly the
        text the whole-tree `mask_summary` would have produced for that same
        leaf. This is the one test that catches future divergence between the
        two maskers.
        """
        summary = {"bluf": "the Zylofenix roadmap review"}
        media_file = _make_file(db_session, normal_user, summary=summary)
        cfg = _cfg()

        result = search_summaries(
            db_session, "roadmap", normal_user.id, organization_id=None, redaction_cfg=cfg
        )
        assert result.results[0].file_uuid == str(media_file.uuid)
        match = result.results[0].matches[0]

        whole_tree_masked = mask_summary(summary, cfg)
        expected = _get_by_path(whole_tree_masked, match.key_path)

        assert match.snippet == expected
        assert "Zylofenix" not in match.snippet, "fixture precondition: masking must have applied"
