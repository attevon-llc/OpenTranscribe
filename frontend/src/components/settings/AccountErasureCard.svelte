<!--
  AccountErasureCard.svelte — "Erase my account" (issue #1354), under Settings > Profile.

  Hidden unless the deployment switched self-service erasure on. Flow: explanation, then a typed
  confirmation plus a re-entry of the password (or an authenticator code), then the request
  receipt with a Cancel button until the scheduled time. The account stays usable during the
  grace period; only an explicit cancel withdraws the request.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import {
    SELF_ERASURE_CONFIRM_WORD,
    cancelSelfErasure,
    getSelfErasureStatus,
    requestSelfErasure,
    selfErasureErrorKey,
    type SelfErasureReceipt,
    type SelfErasureStatus,
  } from '$lib/api/selfErasure';
  import { locale, t } from '$stores/locale';
  import { lastErasureOutcome, openErasureRequest } from '$stores/selfErasure';
  import { toastStore } from '$stores/toast';

  let status: SelfErasureStatus | null = null;
  let loading = true;
  let loadFailed = false;
  let busy = false;

  let typed = '';
  let password = '';
  let mfaCode = '';
  let errorKey = '';

  $: request = status?.request ?? null;
  $: scheduledMs = request?.scheduled_for ? Date.parse(request.scheduled_for) : NaN;
  // Re-evaluated whenever the request changes; the server is the authority once the time passes.
  $: canCancel = request !== null && Number.isFinite(scheduledMs) && Date.now() < scheduledMs;
  $: canSubmit =
    !busy &&
    typed === SELF_ERASURE_CONFIRM_WORD &&
    (password.length > 0 || mfaCode.trim().length > 0);

  function formatWhen(iso: string | null): string {
    if (!iso) return '';
    return new Intl.DateTimeFormat($locale, { dateStyle: 'long', timeStyle: 'short' }).format(
      new Date(iso)
    );
  }

  function setRequest(next: SelfErasureReceipt | null) {
    if (status) status = { ...status, request: next };
    openErasureRequest.set(next);
  }

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      status = await getSelfErasureStatus();
      openErasureRequest.set(status.request);
      lastErasureOutcome.set(status.request ? null : (status.last_outcome ?? null));
    } catch (error) {
      console.error('Failed to read the erasure request status:', error);
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function submit() {
    if (!canSubmit) return;
    busy = true;
    errorKey = '';
    try {
      const receipt = await requestSelfErasure({
        confirm: SELF_ERASURE_CONFIRM_WORD,
        ...(password ? { password } : {}),
        ...(mfaCode.trim() ? { mfa_code: mfaCode.trim() } : {}),
      });
      typed = '';
      password = '';
      mfaCode = '';
      setRequest(receipt);
      toastStore.success($t('settings.accountErasure.requestedToast'));
    } catch (error) {
      errorKey = selfErasureErrorKey(error);
    } finally {
      busy = false;
    }
  }

  async function cancel() {
    if (!request || busy) return;
    busy = true;
    errorKey = '';
    try {
      await cancelSelfErasure(request.ledger_uuid);
      setRequest(null);
      toastStore.success($t('settings.accountErasure.cancelledToast'));
    } catch (error) {
      errorKey = selfErasureErrorKey(error);
      void load();
    } finally {
      busy = false;
    }
  }

  function downloadReceipt() {
    if (!request) return;
    const blob = new Blob([JSON.stringify(request, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `erasure-request-${request.ledger_uuid}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  onMount(() => {
    void load();
  });
</script>

{#if loading}
  <div class="erasure-card" data-testid="account-erasure-card-loading">
    <SettingsSkeleton variant="rows" count={1} label={$t('common.loading')} />
  </div>
{:else if loadFailed}
  <div class="erasure-card" data-testid="account-erasure-card">
    <h4 class="title">{$t('settings.accountErasure.title')}</h4>
    <p class="error" role="alert">{$t('settings.accountErasure.loadError')}</p>
  </div>
{:else if status?.enabled}
  <div class="erasure-card" data-testid="account-erasure-card">
    <h4 class="title">{$t('settings.accountErasure.title')}</h4>

    {#if request}
      <p class="scheduled" data-testid="erasure-scheduled">
        {$t('settings.accountErasure.scheduledFor', { date: formatWhen(request.scheduled_for) })}
      </p>
      <p class="hint">{$t('settings.accountErasure.stillUsable')}</p>

      <dl class="receipt" aria-label={$t('settings.accountErasure.receiptTitle')}>
        <dt>{$t('settings.accountErasure.receiptTitle')}</dt>
        <dd class="muted">
          {$t('settings.accountErasure.requestedAt', { date: formatWhen(request.requested_at) })}
        </dd>
        <dt>{$t('settings.accountErasure.receiptReference')}</dt>
        <dd><code dir="ltr" class="mono">{request.ledger_uuid}</code></dd>
        <dt>{$t('settings.accountErasure.receiptDigest')}</dt>
        <dd><code dir="ltr" class="mono">{request.request_digest}</code></dd>
      </dl>

      <div class="actions">
        <button type="button" class="btn btn-secondary" on:click={downloadReceipt}>
          {$t('settings.accountErasure.downloadReceipt')}
        </button>
        {#if canCancel}
          <button
            type="button"
            class="btn btn-primary"
            data-testid="erasure-cancel"
            disabled={busy}
            on:click={cancel}
          >
            {busy ? $t('settings.accountErasure.cancelling') : $t('settings.accountErasure.cancelButton')}
          </button>
        {/if}
      </div>
    {:else}
      {#if status.last_outcome?.reason === 'last_super_admin'}
        <p class="notice" role="status" data-testid="erasure-blocked">
          {$t('settings.accountErasure.blockedLastSuperAdmin')}
        </p>
      {/if}
      <p class="intro">{$t('settings.accountErasure.intro')}</p>
      <p class="hint">{$t('settings.accountErasure.graceInfo', { days: status.grace_days })}</p>
      <p class="hint">{$t('settings.accountErasure.heldNote')}</p>

      {#if !status.can_reauthenticate}
        <p class="error" role="alert">{$t('settings.accountErasure.reauthUnavailable')}</p>
      {:else}
        <form class="form" on:submit|preventDefault={submit}>
          <label class="field">
            <span>{$t('settings.accountErasure.passwordLabel')}</span>
            <input
              type="password"
              class="form-control"
              autocomplete="current-password"
              bind:value={password}
              data-testid="erasure-password"
            />
          </label>
          <label class="field">
            <span>{$t('settings.accountErasure.mfaLabel')}</span>
            <input
              type="text"
              inputmode="numeric"
              class="form-control"
              autocomplete="one-time-code"
              dir="ltr"
              bind:value={mfaCode}
              data-testid="erasure-mfa"
            />
          </label>
          <label class="field">
            <span>
              {$t('settings.accountErasure.confirmLabel', { word: SELF_ERASURE_CONFIRM_WORD })}
            </span>
            <input
              type="text"
              class="form-control"
              autocomplete="off"
              dir="ltr"
              bind:value={typed}
              data-testid="erasure-confirm"
            />
          </label>
          <div class="actions">
            <button
              type="submit"
              class="btn btn-danger"
              data-testid="erasure-submit"
              disabled={!canSubmit}
            >
              {busy ? $t('settings.accountErasure.requesting') : $t('settings.accountErasure.requestButton')}
            </button>
          </div>
        </form>
      {/if}
    {/if}

    {#if errorKey}
      <p class="error" role="alert" data-testid="erasure-error">{$t(errorKey)}</p>
    {/if}
  </div>
{/if}

<style>
  .erasure-card {
    margin-top: var(--space-4);
    padding: var(--space-4);
    border-radius: var(--radius-xl);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: var(--space-2-5);
  }

  .title {
    margin: 0;
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .intro,
  .hint,
  .scheduled {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .hint,
  .muted {
    color: var(--text-secondary);
  }

  .scheduled {
    font-weight: var(--font-weight-semibold);
  }

  .receipt {
    margin: 0;
    display: grid;
    gap: var(--space-1);
    font-size: var(--font-size-sm);
  }

  .receipt dt {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .receipt dd {
    margin: 0 0 var(--space-1-5);
    min-width: 0;
  }

  .mono {
    display: block;
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    overflow-wrap: anywhere;
    text-align: left;
    color: var(--text-color);
  }

  .form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .btn-danger {
    background: var(--error-color);
    color: var(--text-on-solid);
    border: 1px solid transparent;
    border-radius: var(--radius-md);
    padding: var(--space-2) var(--space-3-5);
    cursor: pointer;
  }

  .btn-danger:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .notice {
    margin: 0;
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-md);
    background: #fef3c7;
    color: #78350f;
    border: 1px solid #f59e0b;
  }

  :global([data-theme='dark']) .notice {
    background: #422006;
    color: #fde68a;
    border-color: #b45309;
  }

  .error {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--error-color);
  }
</style>
