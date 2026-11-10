/**
 * Speaker renaming on the file-detail page: the single rename (from the transcript or a
 * suggestion chip), the linked-profile decision dialog, and the editor's bulk "save all".
 *
 * Every write is optimistic and undone on failure, so nothing renders as saved that the
 * server refused. The page keeps owning the rendered state (#338) and exposes it through
 * {@link SpeakerSaveContext}; the dialog queue and bulk decisions are private to this flow.
 */
import { get } from 'svelte/store';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import { reportHoldRefusal } from '$lib/legalHold/refusal';
import { applySpeakerRename, performSpeakerUpdate } from './speakerRenameWrite';
import { performBulkSave, type BulkDecisions } from './speakerBulkSave';
import { tr, type SpeakerSaveContext } from './speakerSaveContext';
import {
  speakerHasUnsavedChange,
  speakersNeedingProfileDecision,
  validateSpeakerName,
  type ProfileDecision,
  type SpeakerItem,
} from './speakerNames';

type PendingSpeakerUpdate = { speakerId: number | string; newName: string; speaker: SpeakerItem };

export function createSpeakerSaveFlow(ctx: SpeakerSaveContext) {
  let pendingSpeakerUpdate: PendingSpeakerUpdate | null = null;
  // Bulk speaker save confirmation state
  let speakerConfirmationQueue: SpeakerItem[] = [];
  let currentConfirmationIndex = 0;
  let bulkSaveInProgress = false;
  const bulkSaveDecisions: BulkDecisions = new Map();

  function resetBulkState(): void {
    bulkSaveInProgress = false;
    speakerConfirmationQueue = [];
    currentConfirmationIndex = 0;
    bulkSaveDecisions.clear();
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
    await performSpeakerUpdate(ctx, speakerId, newName, 'normal');
  }

  // Show next confirmation in the queue
  function showNextConfirmation() {
    const speaker = speakerConfirmationQueue[currentConfirmationIndex];
    if (speaker) {
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

      await performSpeakerUpdate(ctx, speakerId, newName, decision);
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
      await performBulkSave(ctx, speakersToUpdate);
    } catch (error) {
      console.error('Error saving speaker names:', error);
      if (!reportHoldRefusal(error)) toastStore.error(tr('speakerProfile.saveAllFailed'));
      ctx.setSavingSpeakers(false);
    }
  }

  // Perform bulk save with confirmation decisions
  async function performBulkSaveWithDecisions() {
    try {
      await performBulkSave(ctx, unsavedSpeakers(), bulkSaveDecisions);
      resetBulkState();
    } catch (error) {
      console.error('Error in bulk save with decisions:', error);
      if (!reportHoldRefusal(error)) toastStore.error(tr('speakerProfile.saveAllFailed'));
      ctx.setSavingSpeakers(false);
      resetBulkState();
    }
  }

  return {
    applySpeakerRename: (speakerUuid: string, speakerLabel: string | undefined, newName: string) =>
      applySpeakerRename(ctx, speakerUuid, speakerLabel, newName),
    requestSpeakerRename,
    decideProfile,
    cancelProfileDecision,
    saveAllSpeakerNames,
  };
}
