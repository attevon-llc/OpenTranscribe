<!--
  FileVoiceprintConsent.svelte — the owner's per-file voice identification choice (issue #1356).

  Shows what was recorded for this recording (the uploader's own statement; the product does not
  verify it) and lets the OWNER turn voice identification off for it. Turning it off deletes the
  voiceprints already made from this recording, so it asks first and then follows the purge to the
  end. Turning it back on records the statement only: voiceprints are created by reprocessing.

  Renders nothing for anyone but the owner (the API answers 404 to everyone else).
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import { PrivacyApi, isTerminalPurgeState, type FileVoiceprintConsentState } from '$lib/api/privacy';
  import { resetEffectivePrivacy } from '$stores/privacyPolicy';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage, getErrorStatus } from '$lib/utils/apiError';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';

  export let fileUuid: string;
  /** The state stored on the file. */
  export let state: FileVoiceprintConsentState = 'unspecified';
  /** Only the owner sees the control. */
  export let isOwner = false;

  const POLL_MS = 2000;

  let confirming = false;
  let busy = false;
  let purging = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let alive = true;

  onDestroy(() => {
    alive = false;
    if (timer) clearTimeout(timer);
  });

  async function follow(taskId: string) {
    purging = true;
    try {
      const status = await PrivacyApi.getPurgeStatus(taskId);
      if (!alive) return;
      if (isTerminalPurgeState(status.status)) {
        purging = false;
        if (status.status === 'completed' && status.residual_ok !== false) {
          toastStore.success($t('file.voiceprintConsent.purgeDone'));
        } else {
          toastStore.error($t('file.voiceprintConsent.purgeFailed'));
        }
        return;
      }
      timer = setTimeout(() => void follow(taskId), POLL_MS);
    } catch (error) {
      console.error('Could not read the voiceprint deletion status:', error);
      purging = false;
    }
  }

  async function change(next: 'granted' | 'declined') {
    busy = true;
    try {
      const result = await PrivacyApi.setFileVoiceprintConsent(fileUuid, next);
      state = result.state;
      resetEffectivePrivacy();
      if (result.purge_task_id) void follow(result.purge_task_id);
      if (result.reprocess_to_create_voiceprints) {
        toastStore.success($t('file.voiceprintConsent.grantedReprocess'));
      }
    } catch (error) {
      console.error('Could not change the voice identification choice:', error);
      const status = getErrorStatus(error);
      const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
      if (status === 409 && (detail as { error?: unknown } | undefined)?.error === 'FILE_UNDER_LEGAL_HOLD') {
        toastStore.error($t('file.voiceprintConsent.heldRefusal'));
      } else if (status === 404) {
        toastStore.error($t('file.voiceprintConsent.unavailable'));
      } else if (status === 503 && next === 'declined') {
        // The server saved the choice; only deleting the existing voiceprints could not be queued.
        state = 'declined';
        resetEffectivePrivacy();
        toastStore.error($t('file.voiceprintConsent.purgePending'));
      } else {
        toastStore.error(getErrorMessage(error, $t('file.voiceprintConsent.saveFailed')));
      }
    } finally {
      busy = false;
      confirming = false;
    }
  }

  $: summaryKey =
    state === 'declined'
      ? 'file.voiceprintConsent.stateDeclined'
      : state === 'granted'
        ? 'file.voiceprintConsent.stateGranted'
        : 'file.voiceprintConsent.stateUnspecified';
</script>

{#if isOwner}
  <div class="voiceprint-consent" data-testid="file-voiceprint-consent">
    <div class="text">
      <strong>{$t('file.voiceprintConsent.title')}</strong>
      <span data-testid="file-voiceprint-consent-state">{$t(summaryKey)}</span>
      {#if purging}
        <span class="progress" role="status">{$t('file.voiceprintConsent.purgeRunning')}</span>
      {/if}
    </div>
    {#if state === 'declined'}
      <button type="button" class="btn btn-secondary" disabled={busy} on:click={() => change('granted')}>
        {$t('file.voiceprintConsent.optIn')}
      </button>
    {:else}
      <button
        type="button"
        class="btn btn-secondary"
        disabled={busy}
        data-testid="file-voiceprint-opt-out"
        on:click={() => (confirming = true)}
      >
        {$t('file.voiceprintConsent.optOut')}
      </button>
    {/if}
  </div>
{/if}

<ConfirmationModal
  bind:isOpen={confirming}
  title={$t('file.voiceprintConsent.confirmTitle')}
  message={$t('file.voiceprintConsent.confirmBody')}
  confirmText={$t('file.voiceprintConsent.confirm')}
  cancelText={$t('file.voiceprintConsent.cancel')}
  on:confirm={() => change('declined')}
  on:cancel={() => (confirming = false)}
/>

<style>
  .voiceprint-consent {
    display: flex; align-items: center; justify-content: space-between; gap: 0.75rem;
    padding: 0.625rem 0.75rem; margin: 0.5rem 0;
    border: 1px solid var(--border-color); border-radius: 6px;
    background: var(--surface-color, transparent);
    font-size: 0.8125rem; color: var(--text-secondary);
  }
  .text { display: flex; flex-direction: column; gap: 0.125rem; min-width: 0; }
  .text strong { color: var(--text-primary); font-weight: 600; }
  .progress { color: var(--primary-on-surface); }
</style>
