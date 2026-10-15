"""Lite: move a stored "this server" speaker detection to Automatic where it keeps speakers.

Lite (``DEPLOYMENT_MODE=lite``) is cloud-only for transcription AND speaker detection
(owner decision 2026-10-11, issue #1228). Before that decision a lite user could choose
``transcription_diarization_source = 'local'`` and lite rediarized on its CPU worker; that
path is gone, so every such user's next file would be refused with
``local_diarization_unavailable``.

For the users who would lose nothing, the stored value is rewritten to ``provider``
(Automatic): their active transcription config is a cloud model that returns speakers, so
Automatic gives them speakers from that vendor, still with voiceprints computed on this
server. A user is rewritten only when ALL of:

* this deployment is lite at migration time (``DEPLOYMENT_MODE``, read the way the runtime
  reads it); on the full image the revision is a no-op;
* the stored source is ``local``;
* their ``active_asr_config_id`` points at a ``user_asr_settings`` row whose
  (provider, model) returns speakers, by the FROZEN table below (a revision must not import
  the live catalog, which will change; ``test_v436_migration_consistency`` checks the table
  against today's catalog so it is right when written).

Left alone on purpose, with the plan's blocking issue shown in Settings and the per-file
refusal naming the fix: a text-only active model (Automatic would be refused too,
``automatic_needs_speaker_provider``: the user must pick pyannote.ai or another provider),
and a user with no active cloud config. Only that one ``user_setting`` value changes: no
transcript, speaker, voiceprint or profile is touched, so files diarized on lite's CPU before
the change keep their speakers. A deployment that switches from full to lite later is not
migrated; the runtime refusal covers it with the same sentence. Idempotent: after one pass
no row matches.

Downgrade is a no-op: the rewritten users keep speakers either way, and nothing records which
rows were rewritten, so there is nothing to restore.

Detection: a data-only revision has no schema marker, so the startup runner's arm keys on the
*absence* of matching rows (the v379/v434 shape) and reads the predicate from this file.

COMMUNITY EDITION: a full-image deployment, or a lite one where nobody chose "this server",
is unaffected.

Revision ID: v436_lite_local_speaker_detection_to_automatic
Revises: v434_reset_unusable_pyannote_source
Create Date: 2026-10-11
"""

import os

from alembic import op

revision = "v436_lite_local_speaker_detection_to_automatic"
down_revision = "v434_reset_unusable_pyannote_source"
branch_labels = None
depends_on = None

#: Cloud ASR providers whose every model returns speaker labels, as the catalog said when
#: this revision was written (``services/asr/factory.ASR_PROVIDER_CATALOG``). Frozen here.
PROVIDERS_WITH_SPEAKERS = (
    "assemblyai",
    "aws",
    "azure",
    "deepgram",
    "gladia",
    "google",
    "pyannote",
    "speechmatics",
)

#: Providers whose speakers depend on the model: only these models return them.
MODELS_WITH_SPEAKERS = {"openai": ("gpt-4o-transcribe-diarize",)}

#: The lite users whose "this server" choice is rewritten (``s`` is ``user_setting``). Spelled
#: out rather than formatted from the tuples above; the consistency test pins the two equal.
LITE_LOCAL_WITH_SPEAKERS_WHERE = """
    s.setting_key = 'transcription_diarization_source'
    AND s.setting_value = 'local'
    AND EXISTS (
        SELECT 1
          FROM user_setting a
          JOIN user_asr_settings c ON c.id::text = a.setting_value
         WHERE a.user_id = s.user_id
           AND a.setting_key = 'active_asr_config_id'
           AND (
               c.provider IN ('assemblyai', 'aws', 'azure', 'deepgram', 'gladia', 'google',
                              'pyannote', 'speechmatics')
               OR (c.provider = 'openai' AND c.model_name IN ('gpt-4o-transcribe-diarize'))
           )
    )
"""

RESET_SQL = f"""
    UPDATE user_setting s
       SET setting_value = 'provider', updated_at = now()
     WHERE {LITE_LOCAL_WITH_SPEAKERS_WHERE}
"""

#: Whether any row still matches: the startup runner's detection marker for this revision.
DETECT_SQL = f"SELECT EXISTS(SELECT 1 FROM user_setting s WHERE {LITE_LOCAL_WITH_SPEAKERS_WHERE})"


def deployment_is_lite() -> bool:
    """Whether this deployment is lite (the runtime's own rule: ``DEPLOYMENT_MODE``)."""
    return os.getenv("DEPLOYMENT_MODE", "full").strip().lower() == "lite"


def statements() -> list[str]:
    """What :func:`upgrade` runs here: the rewrite on lite, nothing on the full image."""
    return [RESET_SQL] if deployment_is_lite() else []


def upgrade():
    for statement in statements():
        op.execute(statement)


def downgrade():
    """No-op (see the module docstring): nothing records which rows were rewritten."""
