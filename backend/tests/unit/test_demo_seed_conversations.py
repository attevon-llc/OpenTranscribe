"""Pre-generated chat conversations in the demo seed bundle (#628, P-D).

Validation is exercised on bundles written to ``tmp_path``; the importer is run against real
rows on the savepoint-isolated ``db_session`` and the stored messages are read back through
the response schema the chat API serves them with.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures.speaker_cleanup_seams import make_file
from sqlalchemy import select

from app.models.chat import ChatConversation
from app.schemas.chat import ChatMessageOut
from app.scripts.demo_seed.__main__ import _seed_conversations
from app.scripts.demo_seed.bundle import BundleError
from app.scripts.demo_seed.conversations import load_conversations

CITE = {"item_id": "a", "start_time": 3.0, "end_time": 9.5, "speaker": "Host", "snippet": "hi"}


def _conv(**overrides):
    data = {
        "title": "What was decided?",
        "items": ["a"],
        "messages": [
            {"role": "user", "content": "What was decided?"},
            {
                "role": "assistant",
                "content": "They agreed to ship.",
                "provider": "vllm",
                "model": "m",
                "citations": [CITE],
            },
        ],
    }
    data.update(overrides)
    return data


def _bundle(root: Path, conv: dict | None) -> Path:
    (root / "conversations").mkdir()
    (root / "conversations" / "c.json").write_text(json.dumps(conv))
    (root / "manifest.json").write_text(
        json.dumps({"conversations": [{"id": "c", "file": "conversations/c.json"}]})
    )
    return root


def test_a_valid_conversation_loads(tmp_path):
    (conv,) = load_conversations(_bundle(tmp_path, _conv()), {"a"})

    assert (conv.id, conv.title, conv.item_ids) == ("c", "What was decided?", ["a"])
    assert [m["role"] for m in conv.messages] == ["user", "assistant"]


def test_a_bundle_without_conversations_loads_none(tmp_path):
    (tmp_path / "manifest.json").write_text("{}")

    assert load_conversations(tmp_path, {"a"}) == []


@pytest.mark.parametrize(
    ("conv", "expected"),
    [
        (_conv(items=["zzz"]), "unknown item 'zzz'"),
        (_conv(title=" "), "missing title"),
        (_conv(messages=[{"role": "user", "content": "q"}]), "at least one question"),
        (
            _conv(
                messages=[{"role": "assistant", "content": "x"}, {"role": "user", "content": "q"}]
            ),
            "message 0 must be role 'user'",
        ),
        (
            _conv(
                messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
            ),
            "must record the provider and model",
        ),
        (
            _conv(
                messages=[
                    {"role": "user", "content": "q"},
                    {
                        "role": "assistant",
                        "content": "a",
                        "provider": "p",
                        "model": "m",
                        "citations": [{**CITE, "item_id": "b"}],
                    },
                ]
            ),
            "cites an item outside the scope",
        ),
    ],
)
def test_a_defective_conversation_is_refused(tmp_path, conv, expected):
    with pytest.raises(BundleError) as caught:
        load_conversations(_bundle(tmp_path, conv), {"a", "b"})

    assert any(expected in p and p.startswith("c:") for p in caught.value.problems)


def test_a_conversation_file_outside_the_bundle_is_refused(tmp_path):
    root = _bundle(tmp_path, _conv())
    (root / "manifest.json").write_text(
        json.dumps({"conversations": [{"id": "c", "file": "../outside.json"}]})
    )

    with pytest.raises(BundleError, match="outside the bundle"):
        load_conversations(root, {"a"})


def test_the_importer_stores_a_finished_conversation_the_demo_user_can_read(
    db_session, normal_user, tmp_path
):
    file_row = make_file(db_session, normal_user)
    db_session.commit()
    (conv,) = load_conversations(_bundle(tmp_path, _conv()), {"a"})

    _seed_conversations(db_session, normal_user.id, [conv], {"a": str(file_row.uuid)})
    db_session.commit()

    row = db_session.scalars(
        select(ChatConversation).where(ChatConversation.user_id == normal_user.id)
    ).one()
    assert row.title == "What was decided?"
    assert row.scope["file_uuids"] == [str(file_row.uuid)]
    assert row.last_message_at is not None
    out = [ChatMessageOut.model_validate({**vars(m), "uuid": str(m.uuid)}) for m in row.messages]
    assert [(m.role, m.status) for m in out] == [("user", "complete"), ("assistant", "complete")]
    assert out[1].citations is not None
    (cite,) = out[1].citations
    assert (cite.file_uuid, cite.start_time, cite.end_time, cite.speaker) == (
        str(file_row.uuid),
        3.0,
        9.5,
        "Host",
    )
    assert (out[1].provider, out[1].model) == ("vllm", "m")
