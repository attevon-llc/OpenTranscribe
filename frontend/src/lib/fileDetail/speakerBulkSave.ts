/**
 * The editor's bulk "save all": one PUT per changed speaker, partial failures undone, and the
 * page's names, baseline and captions brought in step with what the server accepted.
 */
import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import { transcriptStore } from '$stores/transcriptStore';
import { clearProcessedVideoCache } from './segmentEdit';
import { renameSpeakersInFile, type SegmentBearingFile } from './segmentSync';
import {
  renameProfileSuggestions,
  savedSpeakerRenames,
  type ProfileDecision,
  type SpeakerItem,
} from './speakerNames';
import { tr, type SpeakerSaveContext } from './speakerSaveContext';

export type BulkDecisions = Map<string | number, { decision: ProfileDecision; newName: string }>;

/** Save every changed speaker, honouring the profile decisions made in the dialog queue. */
export async function performBulkSave(
  ctx: SpeakerSaveContext,
  speakersToUpdate: SpeakerItem[],
  decisions: BulkDecisions = new Map()
) {
  // STEP 1: Optimistic UI updates - immediately update voice suggestions with new names
  const nameChanges = new Map<string, { oldName: string; newName: string }>();

  speakersToUpdate.forEach((speaker) => {
    const decision = decisions.get(speaker.uuid);
    const newName = (speaker.display_name ?? '').trim();

    // If updating a profile globally, track the name change
    if (decision && decision.decision === 'update_profile' && speaker.profile) {
      nameChanges.set(speaker.profile.uuid, { oldName: speaker.profile.name, newName });
    }
  });

  if (nameChanges.size > 0) {
    ctx.setSpeakerList(renameProfileSuggestions(ctx.getSpeakerList(), nameChanges));
  }

  // STEP 2: Update speakers in the backend with decisions
  // Backend returns immediately after saving to PostgreSQL - heavy processing happens in background
  // `allSettled`, not `all`: with `all` a single rejection discarded the outcome of every
  // other speaker, so the page could neither confirm the ones that saved nor undo the
  // one that didn't.
  const results = await Promise.allSettled(
    speakersToUpdate.map((speaker) => {
      const decision = decisions.get(speaker.uuid);
      const payload: { display_name: string; name: string; profile_action?: ProfileDecision } = {
        display_name: (speaker.display_name ?? '').trim(),
        name: speaker.name,
      };

      // Add profile action if there's a decision for this speaker
      if (decision) {
        payload.profile_action = decision.decision;
      }

      return axiosInstance.put(`/speakers/${speaker.uuid}`, payload);
    })
  );

  const failed = speakersToUpdate.filter((_, i) => results[i].status === 'rejected');
  const failedUuids = new Set(failed.map((speaker) => speaker.uuid));
  results.forEach((result, i) => {
    if (result.status === 'rejected') {
      console.error(`Failed to save speaker ${speakersToUpdate[i].uuid}:`, result.reason);
    }
  });

  // Restore the names that did not save, so nothing renders as though it persisted.
  if (failed.length > 0) {
    const baseline = ctx.getBaseline();
    ctx.setSpeakerList(
      ctx
        .getSpeakerList()
        .map((speaker) =>
          failedUuids.has(speaker.uuid)
            ? { ...speaker, display_name: baseline.get(speaker.uuid) ?? speaker.display_name }
            : speaker
        )
    );
  }

  // STEP 4: PostgreSQL updates complete - stop save button spinner immediately!
  ctx.setSavingSpeakers(false);
  ctx.closeSpeakerEditor();
  if (failed.length > 0) {
    toastStore.error(tr('speakerProfile.bulkSavePartialFailure', { count: failed.length }));
  } else if (speakersToUpdate.length > 0) {
    // Only announce success when something was actually attempted and persisted.
    // With an empty `speakersToUpdate` (nothing changed) this used to still fire.
    toastStore.success(tr('speakerProfile.savedSuccess'));
  }

  // Reset original names to current values (no changes after save)
  ctx.setBaseline(
    new Map(
      ctx.getSpeakerList().map((speaker) => [speaker.uuid, (speaker.display_name || '').trim()])
    )
  );

  // STEP 5: Apply the saved names everywhere the page renders them (instant).
  // Every speaker in `speakersToUpdate` that did NOT fail was just persisted —
  // including a clear (see `savedSpeakerRenames`).
  const renames = savedSpeakerRenames(speakersToUpdate, failedUuids);

  renames.forEach((rename) => transcriptStore.updateSpeakerName(rename.uuid, rename.displayName));
  const file = renameSpeakersInFile(ctx.getFile() as SegmentBearingFile, renames);
  ctx.setFile(file);

  // Update captions and clear cache (async, don't block)
  ctx.refreshCaptionTrack('saving speaker names');
  clearProcessedVideoCache(file.uuid);
}
