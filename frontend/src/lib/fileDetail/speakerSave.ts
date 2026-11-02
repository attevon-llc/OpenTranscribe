/**
 * Speaker renaming on the file-detail page: the single rename (from the transcript or a
 * suggestion chip), the linked-profile decision dialog, and the editor's bulk "save all".
 *
 * Every write is optimistic and undone on failure, so nothing renders as saved that the
 * server refused. The page keeps owning the rendered state (#338) and exposes it through
 * {@link SpeakerSaveContext}; the dialog queue and bulk decisions are private to this flow.
 */
import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { getErrorStatus } from '$lib/utils/apiError';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import { transcriptStore } from '$stores/transcriptStore';
import { clearProcessedVideoCache } from './segmentEdit';
import { renameSpeakersInFile, type SegmentBearingFile } from './segmentSync';
import type { SpeakerLoaderContext } from './speakerLoader';
import {
  renameProfileSuggestions,
  savedSpeakerRenames,
  speakerHasUnsavedChange,
  speakersNeedingProfileDecision,
  validateSpeakerName,
  type ProfileDecision,
  type SpeakerItem,
} from './speakerNames';

export interface SpeakerSaveContext extends SpeakerLoaderContext {
  /** Must perform the page's real `file = ...` assignment (see `notificationHandler`). */
  setFile: (file: SegmentBearingFile) => void;
  loadSpeakers: () => Promise<void>;
  setSavingSpeakers: (saving: boolean) => void;
  /** Show (non-null) or close (null) the update-globally / create-new dialog. */
  setProfileConfirmation: (dialog: { title: string; message: string } | null) => void;
  closeSpeakerEditor: () => void;
  isDiarizationDisabled: () => boolean;
  refreshCaptionTrack: (reason: string) => void;
  refreshAnalytics: () => void;
}

type PendingSpeakerUpdate = { speakerId: number | string; newName: string; speaker: SpeakerItem };
type BulkDecisions = Map<string | number, { decision: ProfileDecision; newName: string }>;

export function createSpeakerSaveFlow(ctx: SpeakerSaveContext) {
  let pendingSpeakerUpdate: PendingSpeakerUpdate | null = null;
  // Bulk speaker save confirmation state
  let speakerConfirmationQueue: SpeakerItem[] = [];
  let currentConfirmationIndex = 0;
  let bulkSaveInProgress = false;
  const bulkSaveDecisions: BulkDecisions = new Map();

  const tr = (key: string, options?: Record<string, unknown>) => get(t)(key, options);

  function resetBulkState(): void {
    bulkSaveInProgress = false;
    speakerConfirmationQueue = [];
    currentConfirmationIndex = 0;
    bulkSaveDecisions.clear();
  }

  /**
   * Apply a speaker display-name change everywhere the page renders it.
   *
   * Used for the optimistic write, for rollback, and for the websocket backstop, so all
   * three stay in step. `speakerLabel` is the original `SPEAKER_XX` (`speaker.name`),
   * which is both a match fallback and the value speaker colours hash — it is never
   * rewritten, only read.
   */
  function applySpeakerRename(
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
  async function performSpeakerUpdate(
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

    applySpeakerRename(speakerUuid, speakerLabel, newName);

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
      applySpeakerRename(speakerUuid, speakerLabel, previousName);
      toastStore.error(tr('speakerProfile.saveFailedReverted', { error: errorMessage }));
    }
  }

  /** A single rename (transcript speaker field or a suggestion chip). */
  async function requestSpeakerRename(speakerId: number | string, newName: string) {
    const validation = validateSpeakerName(newName, get(t));
    if (!validation.isValid) {
      toastStore.error(validation.error ?? tr('speakerValidation.nameRequired'));
      return;
    }

    // Find the speaker to check if they have a profile
    const speaker = ctx.getSpeakerList().find((s) => s.uuid === speakerId);

    // Check if this speaker has a profile and the name is changing
    if (speaker && speaker.profile && speaker.profile.name !== newName) {
      // Show confirmation modal for profile update decision
      pendingSpeakerUpdate = { speakerId, newName, speaker };
      ctx.setProfileConfirmation({
        title: tr('speakerProfile.updateTitle'),
        message: tr('speakerProfile.linkedMessage', {
          speakerName: speaker.display_name || speaker.name,
          profileName: speaker.profile.name,
        }),
      });
      return;
    }

    // If no profile or name is the same, proceed with normal update
    await performSpeakerUpdate(speakerId, newName, 'normal');
  }

  // Show next confirmation in the queue
  function showNextConfirmation() {
    if (currentConfirmationIndex < speakerConfirmationQueue.length) {
      const speaker = speakerConfirmationQueue[currentConfirmationIndex];
      pendingSpeakerUpdate = {
        speakerId: speaker.uuid,
        newName: speaker.display_name ?? speaker.name,
        speaker,
      };
      ctx.setProfileConfirmation({
        title: tr('fileDetail.updateSpeakerProfileCounter', {
          current: currentConfirmationIndex + 1,
          total: speakerConfirmationQueue.length,
        }),
        message: tr('fileDetail.profileLinkedMessage', {
          displayName: speaker.display_name || speaker.name,
          profileName: speaker.profile?.name ?? '',
        }),
      });
    }
  }

  /** The dialog's update-globally / create-new answer. */
  async function decideProfile(decision: ProfileDecision) {
    if (bulkSaveInProgress) {
      await handleBulkConfirmation(decision);
    } else if (pendingSpeakerUpdate) {
      const { speakerId, newName } = pendingSpeakerUpdate;

      // Close BEFORE awaiting, as the bulk path already does. The optimistic rename runs
      // synchronously inside performSpeakerUpdate, so the dialog closes and the transcript
      // repaints in the same frame instead of the dialog sitting open through the request.
      ctx.setProfileConfirmation(null);
      pendingSpeakerUpdate = null;

      await performSpeakerUpdate(speakerId, newName, decision);
    }
  }

  /** The dialog was dismissed: abandon the rename, and any bulk save in progress. */
  function cancelProfileDecision() {
    ctx.setProfileConfirmation(null);
    pendingSpeakerUpdate = null;

    if (bulkSaveInProgress) {
      resetBulkState();
      ctx.setSavingSpeakers(false);
    }
  }

  async function handleBulkConfirmation(decision: ProfileDecision) {
    if (!pendingSpeakerUpdate || speakerConfirmationQueue.length === 0) return;

    const { speakerId, newName } = pendingSpeakerUpdate;

    // Store the decision for this speaker
    bulkSaveDecisions.set(speakerId, { decision, newName });

    // Move to next confirmation or finish
    currentConfirmationIndex++;

    if (currentConfirmationIndex < speakerConfirmationQueue.length) {
      showNextConfirmation();
    } else {
      // All confirmations done, proceed with bulk save
      ctx.setProfileConfirmation(null);
      await performBulkSaveWithDecisions();
    }
  }

  function unsavedSpeakers(): SpeakerItem[] {
    const baseline = ctx.getBaseline();
    return ctx.getSpeakerList().filter((speaker) => speakerHasUnsavedChange(speaker, baseline));
  }

  /** The speaker editor's "save all". */
  async function saveAllSpeakerNames() {
    const speakerList = ctx.getSpeakerList();
    if (!speakerList || speakerList.length === 0) return;

    ctx.setSavingSpeakers(true);

    try {
      // Speakers whose display name actually changed since the last load/save —
      // including a change to empty (see speakerHasUnsavedChange). The old filter
      // dropped both empty and SPEAKER_-prefixed CURRENT values regardless of
      // whether they had changed, so "clear a label" issued no PUT at all.
      const speakersToUpdate = unsavedSpeakers();

      if (speakersToUpdate.length === 0) {
        ctx.setSavingSpeakers(false);
        return;
      }

      // Validate non-empty names only — an empty name is a deliberate clear
      // (see speakerHasUnsavedChange), not a validation failure.
      for (const speaker of speakersToUpdate) {
        const trimmed = (speaker.display_name ?? '').trim();
        if (trimmed === '') continue;
        const validation = validateSpeakerName(trimmed, get(t));
        if (!validation.isValid) {
          toastStore.error(`${speaker.name}: ${validation.error}`);
          ctx.setSavingSpeakers(false);
          return;
        }
      }

      const speakersNeedingConfirmation = speakersNeedingProfileDecision(
        speakersToUpdate,
        ctx.isDiarizationDisabled()
      );

      if (speakersNeedingConfirmation.length > 0) {
        // Start bulk confirmation process
        speakerConfirmationQueue = speakersNeedingConfirmation;
        currentConfirmationIndex = 0;
        bulkSaveInProgress = true;
        bulkSaveDecisions.clear();

        showNextConfirmation();
        return;
      }

      // No confirmations needed, proceed with regular save
      await performBulkSave(speakersToUpdate);
    } catch (error) {
      console.error('Error saving speaker names:', error);
      toastStore.error(tr('speakerProfile.saveAllFailed'));
      ctx.setSavingSpeakers(false);
    }
  }

  // Perform bulk save with confirmation decisions
  async function performBulkSaveWithDecisions() {
    try {
      await performBulkSave(unsavedSpeakers(), bulkSaveDecisions);
      resetBulkState();
    } catch (error) {
      console.error('Error in bulk save with decisions:', error);
      toastStore.error(tr('speakerProfile.saveAllFailed'));
      ctx.setSavingSpeakers(false);
      resetBulkState();
    }
  }

  // Perform the actual bulk save operation
  async function performBulkSave(
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

  return {
    applySpeakerRename,
    requestSpeakerRename,
    decideProfile,
    cancelProfileDecision,
    saveAllSpeakerNames,
  };
}
