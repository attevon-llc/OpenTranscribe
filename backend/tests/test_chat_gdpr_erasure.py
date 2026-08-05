"""Chat conversations under GDPR erasure (issue #52).

Chat threads quote transcript content back at the user, so an erasure that
removed the recordings but left the conversations would leave that content
recoverable. Two properties matter:

* account erasure removes conversations even when a legal hold preserves the
  files and the user row (Art. 17(3)(e) keeps the evidence, not the chat), and
* org-member erasure removes ONLY the conversations stamped with that org — an
  org admin has authority over their tenant's data, never over the person's
  personal data.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.core.security import get_password_hash
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.user import User
from app.services.gdpr_erasure_service import erase_org_member_data
from app.services.gdpr_erasure_service import erase_user


def _user(db, label: str) -> User:
    uid = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"{label}_{uid}@example.com",
        full_name=f"{label} user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _org(db, label: str) -> Organization:
    uid = uuid_pkg.uuid4().hex[:8]
    org = Organization(external_org_id=f"org_{label}_{uid}", name=f"{label} Org", is_active=True)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _conversation(db, user, *, org_id=None, title="Chat") -> ChatConversation:
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=org_id,
        title=title,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    db.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role="user",
            content="what did we decide about the contract?",
        )
    )
    db.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role="assistant",
            content="The team agreed to the revised terms [1].",
        )
    )
    db.commit()
    return conversation


def _held_file(db, user) -> MediaFile:
    """A file under legal hold — erasure must preserve it but not the chat."""
    fuuid = uuid_pkg.uuid4()
    media = MediaFile(
        uuid=fuuid,
        filename=f"held_{fuuid.hex[:8]}.mp4",
        storage_path=f"media/test/{fuuid}.mp4",
        content_type="video/mp4",
        file_size=1000,
        user_id=user.id,
        status="completed",
        legal_hold=True,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def test_account_erasure_deletes_conversations(db_session):
    user = _user(db_session, "erase")
    conversation = _conversation(db_session, user)

    summary = erase_user(db_session, user.id)

    assert summary["chat_conversations_deleted"] == 1
    assert (
        db_session.query(ChatConversation).filter(ChatConversation.id == conversation.id).first()
        is None
    )


def test_erasure_cascades_to_the_messages(db_session):
    """The content lives in the messages — an orphaned row would defeat the point."""
    user = _user(db_session, "cascade")
    conversation = _conversation(db_session, user)
    assert (
        db_session.query(ChatMessage).filter(ChatMessage.conversation_id == conversation.id).count()
        == 2
    )

    erase_user(db_session, user.id)

    assert (
        db_session.query(ChatMessage).filter(ChatMessage.conversation_id == conversation.id).count()
        == 0
    )


def test_conversations_are_erased_even_when_a_legal_hold_retains_the_user(db_session):
    """The hold preserves evidence; it does not license keeping the chat."""
    user = _user(db_session, "hold")
    _held_file(db_session, user)
    conversation = _conversation(db_session, user)

    summary = erase_user(db_session, user.id)

    assert summary["legal_holds_skipped"] >= 1
    assert summary["users_deleted"] == 0  # user row retained by the hold
    assert summary["chat_conversations_deleted"] == 1
    assert (
        db_session.query(ChatConversation).filter(ChatConversation.id == conversation.id).first()
        is None
    )


def test_erasure_does_not_touch_another_users_conversations(db_session):
    subject = _user(db_session, "subject")
    bystander = _user(db_session, "bystander")
    _conversation(db_session, subject, title="Subject's")
    theirs = _conversation(db_session, bystander, title="Bystander's")

    erase_user(db_session, subject.id)

    assert (
        db_session.query(ChatConversation).filter(ChatConversation.id == theirs.id).first()
        is not None
    )


def test_erasure_of_a_user_with_no_conversations_reports_zero(db_session):
    user = _user(db_session, "empty")
    summary = erase_user(db_session, user.id)
    assert summary["chat_conversations_deleted"] == 0


def test_org_member_erasure_removes_only_that_orgs_conversations(db_session):
    """An org admin's reach stops at their tenant."""
    org = _org(db_session, "acme")
    member = _user(db_session, "member")

    in_org = _conversation(db_session, member, org_id=org.id, title="Org work")
    personal = _conversation(db_session, member, org_id=None, title="Personal")

    summary = erase_org_member_data(db_session, member.id, org.id)

    assert summary["chat_conversations_deleted"] == 1
    assert (
        db_session.query(ChatConversation).filter(ChatConversation.id == in_org.id).first() is None
    )
    assert (
        db_session.query(ChatConversation).filter(ChatConversation.id == personal.id).first()
        is not None
    )


def test_org_member_erasure_leaves_other_orgs_alone(db_session):
    first = _org(db_session, "first")
    second = _org(db_session, "second")
    member = _user(db_session, "multi")

    in_first = _conversation(db_session, member, org_id=first.id)
    in_second = _conversation(db_session, member, org_id=second.id)

    erase_org_member_data(db_session, member.id, first.id)

    assert (
        db_session.query(ChatConversation).filter(ChatConversation.id == in_first.id).first()
        is None
    )
    assert (
        db_session.query(ChatConversation).filter(ChatConversation.id == in_second.id).first()
        is not None
    )


def test_org_member_erasure_leaves_the_user_row(db_session):
    """Tenant data only — the person's account is not the admin's to delete."""
    org = _org(db_session, "tenant")
    member = _user(db_session, "keepme")
    _conversation(db_session, member, org_id=org.id)

    erase_org_member_data(db_session, member.id, org.id)

    assert db_session.query(User).filter(User.id == member.id).first() is not None
