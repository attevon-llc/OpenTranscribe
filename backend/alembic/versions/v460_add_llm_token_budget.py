"""Running token counters for the LLM token limits (issue #790).

Why
---
LLM usage was recorded (``usage_event``) but nothing bounded it. Owner decision D13: a token
limit per user per UTC day and per instance per UTC month, over every LLM feature, enforced
at the shared ``LLMService`` call seam.

``llm_token_budget`` holds one counter per (scope, user, period). The enforcement is a single
conditional ``UPDATE ... SET used = used + :n WHERE used + :n <= :limit``: under READ COMMITTED
a second writer of the same row waits for the first and re-evaluates its ``WHERE`` against the
updated row (PostgreSQL docs, "Read Committed Isolation Level"), so two concurrent calls
cannot both take the last tokens. ``usage_event`` stays the reporting record.

- ``scope``: ``user_day`` (``user_id`` set, ``period`` ``YYYY-MM-DD``) or ``instance_month``
  (``user_id`` NULL, ``period`` ``YYYY-MM``); both CHECKed.
- ``user_id``: ``ON DELETE CASCADE``, a deleted user leaves no counters.
- ``uq_llm_token_budget_counter``: unique on ``(scope, COALESCE(user_id, 0), period)``, the
  ``ON CONFLICT`` target that creates a counter on first use.

Empty until the first LLM call after the upgrade. No limit applies until an admin sets one
(both default to 0 = unlimited). The detection arm keys on the table, which the single
``CREATE TABLE`` creates together with its CHECKs. The downgrade drops the counters, which
only means the current day and month start again from zero.

Numbered v460 on a lane (v459 is reserved for another lane); chained off v458 until the
coordinator re-chains it at merge.

Revision ID: v460_add_llm_token_budget
Revises: v458_speaker_machine_identity_index
Create Date: 2026-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v460_add_llm_token_budget"
down_revision = "v458_speaker_machine_identity_index"
branch_labels = None
depends_on = None

#: Module-level so a consistency test can replay the real statements.
UPGRADE_SQL = """
    CREATE TABLE IF NOT EXISTS llm_token_budget (
        id SERIAL PRIMARY KEY,
        scope VARCHAR(16) NOT NULL,
        user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
        period VARCHAR(10) NOT NULL,
        used BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        CONSTRAINT ck_llm_token_budget_scope CHECK (scope IN ('user_day', 'instance_month')),
        CONSTRAINT ck_llm_token_budget_subject CHECK ((scope = 'user_day') = (user_id IS NOT NULL)),
        CONSTRAINT ck_llm_token_budget_used CHECK (used >= 0)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_token_budget_counter
        ON llm_token_budget (scope, (COALESCE(user_id, 0)), period);
"""

DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS llm_token_budget;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
