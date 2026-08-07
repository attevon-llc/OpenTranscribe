"""Wire contract for the account-approval admin surface (``v379``).

Kept separate from ``schemas/user.py`` because the pending-queue row is a
different projection from the full user record: it deliberately carries only what
an administrator needs to make the decision (who, from where, when), and adding
those fields to ``User`` would widen every user response in the product.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class PendingAccount(BaseModel):
    """One account awaiting a decision.

    ``auth_type`` is the load-bearing field: "someone self-registered" and "an
    identity provider minted this on first login" are very different things to be
    approving, and they are indistinguishable from the email alone.
    """

    uuid: UUID
    email: str
    full_name: str | None = None
    auth_type: str
    role: str
    #: Whether the deployment has proved control of the address. An unverified
    #: self-registration is the case an approver most needs flagged.
    email_verified: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApprovalDecisionRequest(BaseModel):
    """Optional free-text note recorded in the audit event for the decision."""

    reason: str | None = Field(default=None, max_length=500)


class ApprovalDecisionResponse(BaseModel):
    """The account's state after the decision."""

    uuid: UUID
    email: str
    approval_status: str
    approved_at: datetime | None = None
    #: UUID of the administrator who decided; ``None`` only if that account has
    #: since been deleted (the FK is ``ON DELETE SET NULL``).
    approved_by: UUID | None = None
