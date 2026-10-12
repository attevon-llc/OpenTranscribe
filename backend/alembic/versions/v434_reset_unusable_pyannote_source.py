"""Reset a pyannote.ai speaker-detection choice that never ran to Automatic (issue #1225).

Before #1225 every local-transcription path ignored the stored speaker-detection source
except ``off``: a user with ``transcription_diarization_source = 'pyannote'`` and local
Whisper silently got this server's speaker detection. #1225 makes that combination do what
it says (pyannote.ai runs after local transcription). For a user who never saved a
pyannote.ai key, doing what it says means every file failing with ``pyannote_key_missing``.

So, for exactly those users, the stored value is rewritten to ``provider`` (Automatic),
which with local Whisper resolves to this server's engine: the behaviour they had before,
unchanged. A user is rewritten only when ALL of:

* the stored source is ``pyannote``;
* they have no active ``user_diarization_settings`` row with ``provider = 'pyannote'`` and
  a non-empty ``api_key``;
* their transcription is local: an ``active_asr_config_id`` pointing at a
  ``user_asr_settings`` row whose provider is ``local``, or no pointer to an existing row
  while the deployment's ``ASR_PROVIDER`` fallback is local (read at migration time with
  the runtime's rule: a cloud ``ASR_PROVIDER`` whose key variable is empty falls back to
  local).

Left alone on purpose: a user WITH a key and local ASR (pyannote.ai now runs and bills, as
chosen; see CHANGELOG), and a user with cloud transcription and no key (#1204's
fail-visible refusal is the intended behaviour there). No transcript, speaker, voiceprint
or profile is touched. Idempotent: after one pass no row matches.

Downgrade is a no-op: the rewritten users ran Automatic-equivalent behaviour before and
after, and nothing records which rows were rewritten, so there is nothing to restore.

Detection: a data-only revision has no schema marker, so the startup runner's arm keys on
the *absence* of matching rows (the v379 shape) and reads the predicate from this file,
so the two can never disagree.

COMMUNITY EDITION: a deployment where nobody chose pyannote.ai is unaffected.

Revision ID: v434_reset_unusable_pyannote_source
Revises: v433_add_requested_transcription_options
Create Date: 2026-10-11
"""

import os

from alembic import op

revision = "v434_reset_unusable_pyannote_source"
down_revision = "v433_add_requested_transcription_options"
branch_labels = None
depends_on = None

#: The key variable each cloud ``ASR_PROVIDER`` needs, as the runtime's env fallback reads
#: it at this revision (``services/asr/factory._KEY_REQUIRED``). Snapshotted, not imported:
#: a revision describes the data rule of its own time and never imports app code.
_ENV_KEY_REQUIRED = {
    "deepgram": "DEEPGRAM_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_SPEECH_KEY",
    "speechmatics": "SPEECHMATICS_API_KEY",
    "gladia": "GLADIA_API_KEY",
    "pyannote": "PYANNOTE_API_KEY",
}
_ENV_PROVIDERS = {*_ENV_KEY_REQUIRED, "google", "aws"}

#: The users whose ``pyannote`` choice cannot run and whose behaviour must not change.
#: Module-level so the consistency test and the startup detection arm share one predicate
#: (``s`` is ``user_setting``). ``:env_local`` is :func:`env_asr_is_local`.
UNUSABLE_PYANNOTE_WHERE = """
    s.setting_key = 'transcription_diarization_source'
    AND s.setting_value = 'pyannote'
    AND NOT EXISTS (
        SELECT 1 FROM user_diarization_settings d
         WHERE d.user_id = s.user_id
           AND d.provider = 'pyannote'
           AND d.is_active
           AND coalesce(d.api_key, '') <> ''
    )
    AND (
        EXISTS (
            SELECT 1
              FROM user_setting a
              JOIN user_asr_settings c ON c.id::text = a.setting_value
             WHERE a.user_id = s.user_id
               AND a.setting_key = 'active_asr_config_id'
               AND c.provider = 'local'
        )
        OR (
            :env_local
            AND NOT EXISTS (
                SELECT 1
                  FROM user_setting a
                  JOIN user_asr_settings c ON c.id::text = a.setting_value
                 WHERE a.user_id = s.user_id
                   AND a.setting_key = 'active_asr_config_id'
            )
        )
    )
"""

RESET_SQL = f"""
    UPDATE user_setting s
       SET setting_value = 'provider', updated_at = now()
     WHERE {UNUSABLE_PYANNOTE_WHERE}
"""

#: Whether any row still matches: the startup runner's detection marker for this revision.
DETECT_SQL = f"SELECT EXISTS(SELECT 1 FROM user_setting s WHERE {UNUSABLE_PYANNOTE_WHERE})"


def env_asr_is_local() -> bool:
    """Whether a user with no active config transcribes locally in this deployment."""
    provider = os.getenv("ASR_PROVIDER", "local").strip().lower()
    if provider in ("", "local") or provider not in _ENV_PROVIDERS:
        return True
    key_var = _ENV_KEY_REQUIRED.get(provider)
    return key_var is not None and not os.getenv(key_var, "").strip()


def upgrade():
    import sqlalchemy as sa

    op.get_bind().execute(sa.text(RESET_SQL), {"env_local": env_asr_is_local()})


def downgrade():
    """No-op (see the module docstring): nothing records which rows were rewritten."""
