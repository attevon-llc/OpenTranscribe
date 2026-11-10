<!--
  PurgeConfirmDialog.svelte — the one confirmation for an irreversible privacy purge (#1280).

  A switch only HIDES data; this deletes it. So the destructive button stays disabled until the
  person has ticked "I understand" AND typed the literal word the API requires (`PURGE`), and the
  copy says what goes, what stays, and that copies may outlive the purge. After the request is
  accepted it follows the purge task (the server runs a second pass about a minute later) and
  reports counts only, never names.

  The caller supplies `start`, the one API call for its scope (own data, organization, instance),
  so this component knows nothing about who is purging.

  When the purge ends it shows what the server MEASURED: the count removed per store, whether the
  check that follows found anything still there (a purge that left something is a failure, and a
  check that could not run is "not verified", never "clean"), and the audit events that record it.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import {
    PrivacyApi,
    PURGE_CONFIRM_WORD,
    PURGE_REASON_FILES_PROTECTED,
    PURGE_REASON_INTERRUPTED,
    isTerminalPurgeState,
    type PurgeAccepted,
    type PurgeStatus,
    type PurgeTarget,
  } from '$lib/api/privacy';
  import {
    privacyErrorKey,
    purgeCountTotal,
    purgeProtectedTotal,
    purgeStoreTotal,
    purgeVerification,
  } from '$lib/settings/privacyPolicy';
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
  $: verification = status !== null ? purgeVerification(status) : null;
  $: protectedTotal = status !== null ? purgeProtectedTotal(status.counts) : 0;

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
      status = {
        task_id: accepted.task_id,
        status: 'pending',
        counts: null,
        residual: null,
        residual_ok: null,
        residual_unverified: [],
        audit: [],
      };
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
        {:else if status.status === 'failed' && status.reason === PURGE_REASON_FILES_PROTECTED}
          <p role="status" data-testid="purge-all-protected">{$t('privacy.purge.allProtected')}</p>
        {:else if status.status === 'failed' && status.reason === PURGE_REASON_INTERRUPTED}
          <p class="error" role="alert" data-testid="purge-interrupted">
            {$t('privacy.purge.interrupted')}
          </p>
        {:else if status.status === 'failed'}
          <p class="error" role="alert">{$t('privacy.purge.failed')}</p>
        {:else if status.status === 'skipped'}
          <p>{$t('privacy.purge.skipped')}</p>
        {:else}
          <p>{$t('privacy.purge.running')}</p>
        {/if}
        {#if finished && status.counts}
          <ul class="results" data-testid="purge-results" aria-label={$t('privacy.purge.resultsLabel')}>
            <li>{$t('privacy.purge.storePostgres', { count: purgeStoreTotal(status.counts, 'postgres') })}</li>
            <li>
              {$t('privacy.purge.storeOpensearch', {
                count: purgeStoreTotal(status.counts, 'opensearch'),
              })}
            </li>
            {#if protectedTotal > 0}
              <li data-testid="purge-protected">
                {$t('privacy.purge.protectedSkipped', { count: protectedTotal })}
              </li>
            {/if}
          </ul>
        {/if}
        {#if finished && verification}
          {#if verification.state === 'verified'}
            <p class="verified" data-testid="purge-verification">
              {$t('privacy.purge.residualOk')}
            </p>
          {:else if verification.state === 'verified_except_protected'}
            <p class="verified" data-testid="purge-verification">
              {$t('privacy.purge.residualOkExceptProtected')}
            </p>
          {:else if verification.state === 'remaining'}
            <p class="error" role="alert" data-testid="purge-verification">
              {$t('privacy.purge.residualFound', { count: verification.remaining })}
            </p>
          {:else if verification.state === 'unverified'}
            <p class="error" role="alert" data-testid="purge-verification">
              {$t('privacy.purge.residualUnverified', { count: verification.unverified })}
            </p>
          {/if}
        {/if}
        {#if finished && (status.audit ?? []).length > 0}
          <div class="kept" data-testid="purge-audit">
            <p>{$t('privacy.purge.auditRef', { task: status.task_id })}</p>
            <ul class="events">
              {#each status.audit as ref (ref.event_type)}
                <li><code dir="ltr">{ref.event_type}</code></li>
              {/each}
            </ul>
          </div>
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
    gap: var(--space-3);
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  p {
    margin: 0;
    line-height: 1.5;
  }

  .deleted {
    margin: 0;
    padding-inline-start: var(--space-5);
    line-height: 1.5;
  }

  .kept,
  small {
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .warning {
    display: flex;
    gap: var(--space-2);
    align-items: flex-start;
    padding: var(--space-2-5) var(--space-3);
    border-radius: var(--radius-md);
    border: 1px solid var(--error-color);
    background: rgba(220, 38, 38, 0.08);
    font-weight: var(--font-weight-medium);
  }

  .warning :global(svg) {
    flex-shrink: 0;
    margin-top: 0.1875rem;
    color: var(--error-text);
  }

  .understand {
    display: flex;
    gap: var(--space-2);
    align-items: flex-start;
    cursor: pointer;
  }

  .understand input {
    margin-top: var(--space-1);
  }

  .typed {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-weight: var(--font-weight-medium);
  }

  .typed input {
    max-width: 14rem;
    font-family: monospace;
    text-align: start;
  }

  .error {
    color: var(--error-text);
  }

  .verified {
    color: var(--color-success-text);
  }

  .results {
    margin: 0;
    padding-inline-start: var(--space-5);
    line-height: 1.5;
  }

  .events {
    margin: var(--space-1) 0 0;
    padding: 0;
    list-style: none;
  }

  code {
    font-size: var(--font-size-sm);
    background: var(--surface-secondary);
    border-radius: var(--radius-sm);
    padding: 0 var(--space-1);
    white-space: nowrap;
  }

  :global([data-theme='dark']) .warning {
    background: rgba(248, 113, 113, 0.12);
  }
</style>
