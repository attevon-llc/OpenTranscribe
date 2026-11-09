/**
 * The capability/role inputs each Settings section's tab gating reads, derived once from the
 * server-driven capability state. Each shape is the `*Access` type of the matching `*Tabs` module.
 */
import type { CapabilitiesState } from '$stores/capabilities';
import type { TranscriptionAccess } from '$lib/settings/transcriptionTabs';
import type { SpeakerIdAccess } from '$lib/settings/speakerIdentificationTabs';
import type { DataAccess } from '$lib/settings/dataManagementTabs';
import type { StatusAccess } from '$lib/settings/fileStatusTabs';
import type { PrivacyRedactionAccess } from '$lib/settings/privacyRedactionTabs';
import { capOn } from '$lib/settings/sidebarSections';

export interface SectionAccess {
  transcription: TranscriptionAccess;
  speakerId: SpeakerIdAccess;
  data: DataAccess;
  status: StatusAccess;
  privacy: PrivacyRedactionAccess;
}

export function buildSectionAccess(
  capState: CapabilitiesState,
  isAdmin: boolean,
  isSuperAdmin: boolean
): SectionAccess {
  return {
    transcription: {
      prefsCap: capOn(capState, 'transcription.prefs'),
      asrCap: capOn(capState, 'asr.user_providers'),
      modelSelectionCap: capOn(capState, 'asr.model_selection'),
      vocabCap: capOn(capState, 'vocab.user'),
    },
    speakerId: {
      isAdmin,
      isSuperAdmin,
      prefsCap: capOn(capState, 'transcription.prefs'),
      engineCap: capOn(capState, 'engine.settings'),
      migrationCap: capOn(capState, 'speaker_attributes.migration'),
    },
    data: {
      isAdmin,
      isSuperAdmin,
      retentionCap: capOn(capState, 'admin.retention'),
      backupCap: capOn(capState, 'admin.backup'),
      integrityCap: capOn(capState, 'admin.data_integrity'),
      searchCap: capOn(capState, 'admin.search_indexing'),
      embeddingCap: capOn(capState, 'admin.embedding_migration'),
    },
    status: {
      isAdmin,
      taskHealthCap: capOn(capState, 'admin.task_health'),
    },
    privacy: {
      isAdmin,
      isSuperAdmin,
      userCap: capOn(capState, 'redaction.user'),
      policyCap: capOn(capState, 'redaction.policy'),
    },
  };
}
