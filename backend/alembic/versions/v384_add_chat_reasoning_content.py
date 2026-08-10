"""Add ``chat_message.reasoning_content`` for the collapsible reasoning display.

A growing set of LLM providers stream their intermediate reasoning/"thinking"
separately from the final answer (vLLM's ``reasoning_content``, Anthropic's
extended thinking, Ollama's ``message.thinking``, or an inline ``<think>`` tag
fallback — see ``app/services/llm_stream.py``). The chat UI shows that text in a
collapsed-by-default block above the final answer, matching the Open WebUI
pattern.

This revision persists it (rather than treating it as stream-only/ephemeral) so
reasoning is still visible when a conversation is reloaded, for parity with the
final answer itself. ``reasoning_content`` is a plain nullable ``TEXT`` column,
identical in shape to the existing ``content`` column: NULL for every user
message and for any assistant reply whose provider never streamed one — which is
every row that predates this revision, and every row from a provider/model with
no reasoning output. Nothing about ``content`` changes; reasoning is additive and
strictly separate so it can never leak into the rendered answer.

COMMUNITY EDITION: a deployment that never uses a reasoning-capable model or
provider has an entirely NULL column and no behavior change at all.

All SQL is idempotent (``IF NOT EXISTS``) for safe re-run on partially-migrated
databases.

Revision ID: v384_add_chat_reasoning_content
Revises: v383_saml_auth_type
Create Date: 2026-08-09
"""

from alembic import op

revision = "v384_add_chat_reasoning_content"
down_revision = "v383_saml_auth_type"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS reasoning_content TEXT")


def downgrade():
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS reasoning_content")
