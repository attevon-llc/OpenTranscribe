"""``POST /asr-settings/local-model/set`` accepts only downloaded catalog models (#1246).

Before: any string containing ``/`` was accepted, every discovery short name (``distil-*``) was
accepted, and the model was never checked to be on disk, so the GPU worker would try to download
it inside ``preload_models`` on restart or start unloaded. Now the id must be a catalog id (or an
alias of one) and its repository must be complete in the hub cache.
"""

from __future__ import annotations

import pytest
from fixtures.hf_cache import make_complete_repo
from fixtures.hf_cache import make_partial_repo

from app.models.system_settings import SystemSettings

SET = "/api/asr-settings/local-model/set"
LARGE_V3 = "Systran/faster-whisper-large-v3"
CRISPER = "nyrahealth/faster_CrisperWhisper"


@pytest.fixture(autouse=True)
def _full_deployment(pipeline_env):
    """Pin a full (non-lite) deployment so the lite refusal cannot mask the guard."""
    pipeline_env("full")


def _stored(db_session) -> str | None:
    db_session.expire_all()
    row = db_session.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
    return None if row is None else str(row.value)


def _post(client, headers, name):
    return client.post(SET, json={"model_name": name}, headers=headers)


@pytest.mark.parametrize(
    "name",
    ["distil-large-v3", "someone/else-ct2", "Systran/faster-whisper-large-v3", "turbo", "large"],
)
def test_a_non_catalog_name_is_refused_with_a_code(
    client, super_admin_token_headers, db_session, hf_cache, name
):
    make_complete_repo(hf_cache, LARGE_V3)

    response = _post(client, super_admin_token_headers, name)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == {
        "code": "unknown_local_model",
        "message": "This model is not in the list of supported local models.",
    }
    assert _stored(db_session) is None


def test_a_catalog_model_that_is_not_downloaded_is_refused(
    client, super_admin_token_headers, db_session, hf_cache
):
    response = _post(client, super_admin_token_headers, "large-v3")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "local_model_not_downloaded",
        "message": "Download this model before activating it.",
    }
    assert _stored(db_session) is None


def test_a_partial_download_is_not_downloaded(
    client, super_admin_token_headers, db_session, hf_cache
):
    make_partial_repo(hf_cache, LARGE_V3)

    response = _post(client, super_admin_token_headers, "large-v3")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "local_model_not_downloaded"
    assert _stored(db_session) is None


def test_a_downloaded_catalog_model_is_stored(
    client, super_admin_token_headers, db_session, hf_cache
):
    make_complete_repo(hf_cache, LARGE_V3)

    response = _post(client, super_admin_token_headers, "large-v3")

    assert response.status_code == 200, response.text
    assert response.json()["model_name"] == "large-v3"
    assert _stored(db_session) == "large-v3"


@pytest.mark.parametrize(
    "alias", ["crisperwhisper", "faster_crisperwhisper", "nyrahealth/CrisperWhisper"]
)
def test_a_crisperwhisper_alias_is_stored_as_the_ct2_repo_id(
    client, super_admin_token_headers, db_session, hf_cache, alias
):
    make_complete_repo(hf_cache, CRISPER)

    response = _post(client, super_admin_token_headers, alias)

    assert response.status_code == 200, response.text
    assert response.json()["model_name"] == CRISPER
    assert _stored(db_session) == CRISPER


def test_an_alias_of_a_model_that_is_not_downloaded_is_refused(
    client, super_admin_token_headers, hf_cache
):
    response = _post(client, super_admin_token_headers, "crisperwhisper")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "local_model_not_downloaded"


def test_a_plain_admin_is_refused(client, admin_token_headers, hf_cache):
    make_complete_repo(hf_cache, LARGE_V3)

    assert _post(client, admin_token_headers, "large-v3").status_code == 403


def test_on_lite_the_lite_refusal_wins_over_the_guard(
    client, super_admin_token_headers, hf_cache, pipeline_env
):
    pipeline_env("lite")

    response = _post(client, super_admin_token_headers, "large-v3")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "local_asr_unavailable"
