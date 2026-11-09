"""Every outcome of ``ProfileEmbeddingService.update_profile_embedding``, strict and lenient.

Strict mode must raise whenever the stored embedding was not replaced by one computed from the
current members (issue #1214); lenient mode returns False (or True for an emptied profile).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.profile_embedding_service import ProfileEmbeddingRecomputeError
from app.services.profile_embedding_service import ProfileEmbeddingService

_P = "app.services.profile_embedding_service."


def _db(profile, speakers):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = profile
    db.query.return_value.filter.return_value.all.return_value = speakers
    return db


def _profile():
    return SimpleNamespace(id=5, user_id=1, organization_id=None)


def _speaker(uuid="s1", media_file_id=10):
    return SimpleNamespace(uuid=uuid, media_file_id=media_file_id)


def _run(db, *, strict, enabled=True, embeddings=(), declined=(), stored=True):
    decision = SimpleNamespace(enabled=enabled, locked_by="org")
    privacy = MagicMock()
    privacy.return_value.voiceprints_for_scope.return_value = decision
    with (
        patch(_P + "file_ids_in_scope", return_value=[]),
        patch(_P + "declined_file_ids", return_value=set(declined)),
        patch(_P + "PrivacyView", privacy),
        patch(_P + "get_speaker_embedding", side_effect=list(embeddings)),
        patch(_P + "_process_profile_with_no_speakers") as cleared,
        patch(_P + "_process_profile_with_embeddings", return_value=stored) as written,
    ):
        try:
            result = ProfileEmbeddingService.update_profile_embedding(db, 5, strict=strict)
        finally:
            run = SimpleNamespace(cleared=cleared, written=written)
    return result, run


@pytest.mark.parametrize("strict", [False, True])
def test_missing_profile(strict):
    db = _db(None, [])
    if strict:
        with pytest.raises(ProfileEmbeddingRecomputeError, match="profile 5 not found"):
            _run(db, strict=True)
    else:
        assert _run(db, strict=False)[0] is False


def test_emptied_profile_is_cleared_and_committed_lenient_returns_true():
    db = _db(_profile(), [])
    result, run = _run(db, strict=False)
    assert result is True
    run.cleared.assert_called_once()
    db.commit.assert_called_once()


def test_emptied_profile_strict_raises_after_the_clear():
    db = _db(_profile(), [])
    with pytest.raises(ProfileEmbeddingRecomputeError, match="has no members"):
        _run(db, strict=True)
    db.commit.assert_called_once()


def test_declined_files_never_enter_the_average():
    db = _db(_profile(), [_speaker("a", 10), _speaker("b", 11)])
    result, run = _run(db, strict=False, embeddings=[[1.0]], declined=[11])
    assert result is True
    assert run.written.call_args.args[2] == [[1.0]]


@pytest.mark.parametrize("strict", [False, True])
def test_voiceprints_disabled_by_policy(strict):
    db = _db(_profile(), [_speaker()])
    if strict:
        with pytest.raises(ProfileEmbeddingRecomputeError, match="disabled by privacy policy"):
            _run(db, strict=True, enabled=False)
    else:
        result, run = _run(db, strict=False, enabled=False)
        assert result is False
        run.written.assert_not_called()


@pytest.mark.parametrize("strict", [False, True])
def test_no_readable_member_voiceprint(strict):
    db = _db(_profile(), [_speaker("a"), _speaker("b")])
    if strict:
        with pytest.raises(ProfileEmbeddingRecomputeError, match="no member voiceprint"):
            _run(db, strict=True, embeddings=[None, []])
    else:
        assert _run(db, strict=False, embeddings=[None, []])[0] is False


@pytest.mark.parametrize("strict", [False, True])
def test_refused_write_commits_then_reports_failure(strict):
    db = _db(_profile(), [_speaker()])
    if strict:
        with pytest.raises(ProfileEmbeddingRecomputeError, match="refused the embedding write"):
            _run(db, strict=True, embeddings=[[1.0]], stored=False)
    else:
        assert _run(db, strict=False, embeddings=[[1.0]], stored=False)[0] is False
    db.commit.assert_called_once()


def test_unreadable_members_are_skipped_but_the_rest_are_averaged():
    db = _db(_profile(), [_speaker("a"), _speaker("b")])
    result, run = _run(db, strict=False, embeddings=[None, [2.0]])
    assert result is True
    assert run.written.call_args.args[2] == [[2.0]]
    db.commit.assert_called_once()


@pytest.mark.parametrize("strict", [False, True])
def test_unexpected_failure_rolls_back(strict):
    db = _db(_profile(), [_speaker()])
    db.query.side_effect = RuntimeError("db down")
    if strict:
        with pytest.raises(ProfileEmbeddingRecomputeError, match="db down"):
            _run(db, strict=True)
    else:
        assert _run(db, strict=False)[0] is False
    db.rollback.assert_called_once()
