/**
 * The single speaker rename write: the optimistic page update, the PUT, and the undo on failure.
 *
 * Every write is optimistic and undone on failure, so nothing renders as saved that the
 * server refused.
 */
import axiosInstance from '$lib/axios';
import { getErrorStatus } from '$lib/utils/apiError';
import { toastStore } from '$stores/toast';
import { transcriptStore } from '$stores/transcriptStore';
import { reportHoldRefusal } from '$lib/legalHold/refusal';
import { renameSpeakersInFile, type SegmentBearingFile } from './segmentSync';
import type { ProfileDecision } from './speakerNames';
import { tr, type SpeakerSaveContext } from './speakerSaveContext';

/**
 * Apply a speaker display-name change everywhere the page renders it.
 *
 * Used for the optimistic write, for rollback, and for the websocket backstop, so all
 * three stay in step. `speakerLabel` is the original `SPEAKER_XX` (`speaker.name`),
 * which is both a match fallback and the value speaker colours hash — it is never
 * rewritten, only read.
 */
export function applySpeakerRename(
  ctx: SpeakerSaveContext,
  speakerUuid: string,
  speakerLabel: string | undefined,
  newName: string
) {
  ctx.setSpeakerList(
    ctx
      .getSpeakerList()
      .map((speaker) =>
        speaker.uuid === speakerUuid ? { ...speaker, display_name: newName } : speaker
      )
  );
  // Feeds the transcript store's readers, which read the store rather than `file`.
  transcriptStore.updateSpeakerName(speakerUuid, newName);
  ctx.setFile(
    renameSpeakersInFile(ctx.getFile() as SegmentBearingFile, [
      { uuid: speakerUuid, label: speakerLabel, displayName: newName },
    ])
  );
  // The caption track is NOT refreshed here: it comes from the server, and this runs
  // before the rename is saved. Callers refresh it once the server has the new name.
}

// Perform the actual speaker update with the specified action
export async function performSpeakerUpdate(
  ctx: SpeakerSaveContext,
  speakerId: number | string,
  newName: string,
  action: 'normal' | ProfileDecision
) {
  const speakerUuid = String(speakerId);
  const speaker = ctx.getSpeakerList().find((s) => s.uuid === speakerId);
  if (!speaker?.uuid) return;

  // Captured before the optimistic write so a failed save can be undone.
  const previousName = speaker.display_name || '';
  const speakerLabel = speaker.name;

  applySpeakerRename(ctx, speakerUuid, speakerLabel, newName);

  // Persist to database with the action decision
  try {
    const payload: { display_name: string; profile_action?: ProfileDecision } = {
      display_name: newName,
      // NEVER update 'name' field - it contains the original speaker ID for color consistency
    };

    // Add profile action if needed
    if (action !== 'normal') {
      payload.profile_action = action;
    }

    await axiosInstance.put(`/speakers/${speaker.uuid}`, payload);

    // Postgres has committed — confirm now rather than after the refresh below.
    const successMessage =
      action === 'update_profile'
        ? tr('speakerProfile.updatedGlobally', { name: newName })
        : action === 'create_new_profile'
          ? tr('speakerProfile.newCreated', { name: newName })
          : tr('speakerProfile.renamed', { name: newName });

    toastStore.success(successMessage);
    ctx.refreshCaptionTrack('speaker rename');

    // Move the change-detection baseline with it, or the unsaved-changes indicator
    // lights up for a name that just saved.
    ctx.setBaseline(new Map(ctx.getBaseline()).set(speakerUuid, newName.trim()));

    // Speaker labels appear in talk-time and turn-taking analytics.
    ctx.refreshAnalytics();

    // Refresh speakers to get updated profile data from GET endpoint
    // This ensures speaker.profile.name is current before the next edit
    await ctx.loadSpeakers();
  } catch (error: unknown) {
    console.error('Failed to update speaker name in database:', error);

    // Show user-friendly error with option to retry
    const status = getErrorStatus(error);
    const errorMessage =
      status === 404
        ? tr('speakerProfile.notFound')
        : status === 403
          ? tr('speakerProfile.permissionDenied')
          : tr('speakerProfile.saveFailed');

    // Undo the optimistic write. Leaving it would render the new name across the
    // transcript, subtitles, modal and exports as though it had saved.
    applySpeakerRename(ctx, speakerUuid, speakerLabel, previousName);
    if (!reportHoldRefusal(error)) {
      toastStore.error(tr('speakerProfile.saveFailedReverted', { error: errorMessage }));
    }
  }
}
