<!--
  PurgeConfirmDialog.svelte — the one confirmation for an irreversible privacy purge (#1280).

  A switch only HIDES data; this deletes it. So the destructive button stays disabled until the
  person has ticked "I understand" AND typed the literal word the API requires (`PURGE`), and the
  copy says what goes, what stays, and that copies may outlive the purge. After the request is
  accepted it follows the purge task (the server runs a second pass about a minute later) and
  reports counts only, never names.

  The caller supplies `start`, the one API call for its scope (own data, organization, instance),
  so this component knows nothing about who is purging.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import {
    PrivacyApi,
    PURGE_CONFIRM_WORD,
    isTerminalPurgeState,
    type PurgeAccepted,
    type PurgeStatus,
    type PurgeTarget,
  } from '$lib/api/privacy';
  import { privacyErrorKey, purgeCountTotal } from '$lib/settings/privacyPolicy';
  import { t } from '$stores/locale';

  export let isOpen = false;
  export let onClose: () => void = () => {};
  export let targets: PurgeTarget[] = [];
  /** Who loses the data, already translated ("your organization", "your own recordings"). */
  export let scopeLabel = '';
  /** Issues the one request for this scope. A rejection is shown inside the dialog. */
  export let start: () => Promise<PurgeAccepted>;
  /** Milliseconds between status reads. */
  export let pollMs = 3000;
  /** Stop following the task after this long; the purge itself keeps running server-side. */
  export let maxPollMs = 5 * 60 * 1000;

  let understood = false;
  let typed = '';
  let busy = false;
  let errorKey = '';
  let status: PurgeStatus | null = null;
  let following = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let wasOpen = false;
  let runId = 0;

  $: confirmed = understood && typed === PURGE_CONFIRM_WORD;
  $: canDelete = confirmed && !busy && !following && status === null;
  $: finished = status !== null && isTerminalPurgeState(status.status);

  // Every opening starts from a clean slate: a stale ticked box must never pre-arm a delete.
  function syncOpen(open: boolean) {
    if (open === wasOpen) return;
    wasOpen = open;
    if (open) reset();
    else stopFollowing();
  }
  $: syncOpen(isOpen);

  function reset() {
    understood = false;
    typed = '';
    busy = false;
    errorKey = '';
    status = null;
    following = false;
    runId += 1;
  }

  function stopFollowing() {
    runId += 1;
    if (timer) clearTimeout(timer);
    timer = null;
  }

  async function confirm() {
    if (!canDelete) return;
    busy = true;
    errorKey = '';
    try {
      const accepted = await start();
      status = { task_id: accepted.task_id, status: 'pending', counts: null };
      follow(accepted.task_id);
    } catch (error) {
      console.error('Privacy purge request failed:', error);
      errorKey = privacyErrorKey(error, 'privacy.purge.error');
    } finally {
      busy = false;
    }
  }

  function follow(taskId: string) {
    following = true;
    const mine = runId;
    const deadline = Date.now() + maxPollMs;
    const tick = async () => {
      if (mine !== runId) return;
      try {
        const next = await PrivacyApi.getPurgeStatus(taskId);
        if (mine !== runId) return;
        // A reply without a state is a failed read, not a state: keep polling.
        if (!next || typeof next.status !== 'string') throw new Error('malformed purge status');
        status = next;
        if (isTerminalPurgeState(next.status)) {
          following = false;
          return;
        }
      } catch (error) {
        console.error('Reading the purge status failed:', error);
      }
      if (Date.now() >= deadline) {
        following = false;
        return;
      }
      timer = setTimeout(tick, pollMs);
    };
    void tick();
  }

  onDestroy(stopFollowing);
</script>

<BaseModal
  {isOpen}
  {onClose}
  size="md"
  title={$t('privacy.purge.title')}
  closeOnBackdropClick={false}
>
  <div class="purge" data-testid="purge-dialog">
    {#if status === null}
      <p class="lead">{$t('privacy.purge.lead', { scope: scopeLabel })}</p>

      <ul class="deleted" aria-label={$t('privacy.purge.deletedLabel')}>
        {#if targets.includes('voiceprints')}
          <li>{$t('privacy.purge.deletesVoiceprints')}</li>
        {/if}
        {#if targets.includes('inferred_gender')}
          <li>{$t('privacy.purge.deletesGender')}</li>
        {/if}
      </ul>

      <p class="kept">{$t('privacy.purge.kept')}</p>

      <p class="warning" role="note">
        <MenuIcon name="warning" size={16} />
        <span>{$t('privacy.purge.irreversible')}</span>
      </p>

      <label class="understand">
        <input type="checkbox" bind:checked={understood} disabled={busy} />
        <span>{$t('privacy.purge.understand')}</span>
      </label>

      <label class="typed">
        <span>{$t('privacy.purge.typeToConfirm', { word: PURGE_CONFIRM_WORD })}</span>
        <input
          type="text"
          bind:value={typed}
          disabled={busy}
          autocomplete="off"
          autocapitalize="off"
          spellcheck="false"
          dir="ltr"
          aria-describedby="purge-type-hint"
        />
      </label>
      <small id="purge-type-hint">{$t('privacy.purge.typeHint')}</small>

      {#if errorKey}
        <p class="error" role="alert">{$t(errorKey)}</p>
      {/if}
    {:else}
      <div class="progress" aria-live="polite">
        {#if status.status === 'completed'}
          <p>{$t('privacy.purge.completed', { total: purgeCountTotal(status.counts) })}</p>
        {:else if status.status === 'failed'}
          <p class="error" role="alert">{$t('privacy.purge.failed')}</p>
        {:else if status.status === 'skipped'}
          <p>{$t('privacy.purge.skipped')}</p>
        {:else}
          <p>{$t('privacy.purge.running')}</p>
        {/if}
        {#if !finished && !following}
          <p class="kept">{$t('privacy.purge.stillRunning')}</p>
        {/if}
      </div>
    {/if}
  </div>

  <svelte:fragment slot="footer">
    <button class="btn btn-secondary" type="button" on:click={onClose}>
      {status === null ? $t('common.cancel') : $t('common.close')}
    </button>
    {#if status === null}
      <button
        class="btn btn-danger"
        type="button"
        disabled={!canDelete}
        on:click={confirm}
        data-testid="purge-confirm"
      >
        {busy ? $t('privacy.purge.deleting') : $t('privacy.purge.confirm')}
      </button>
    {/if}
  </svelte:fragment>
</BaseModal>

<style>
  .purge {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    font-size: 0.875rem;
    color: var(--text-color);
  }

  p {
    margin: 0;
    line-height: 1.5;
  }

  .deleted {
    margin: 0;
    padding-inline-start: 1.25rem;
    line-height: 1.5;
  }

  .kept,
  small {
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .warning {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    padding: 0.625rem 0.75rem;
    border-radius: 6px;
    border: 1px solid var(--error-color, #dc2626);
    background: rgba(220, 38, 38, 0.08);
    font-weight: var(--font-weight-medium, 500);
  }

  .warning :global(svg) {
    flex-shrink: 0;
    margin-top: 0.1875rem;
    color: var(--error-color, #dc2626);
  }

  .understand {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    cursor: pointer;
  }

  .understand input {
    margin-top: 0.25rem;
  }

  .typed {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-weight: var(--font-weight-medium, 500);
  }

  .typed input {
    max-width: 14rem;
    font-family: monospace;
    text-align: start;
  }

  .error {
    color: var(--error-color, #dc2626);
  }

  :global([data-theme='dark']) .warning {
    background: rgba(248, 113, 113, 0.12);
  }
</style>
