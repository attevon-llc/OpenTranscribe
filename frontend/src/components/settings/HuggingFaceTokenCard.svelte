<!--
  HuggingFaceTokenCard.svelte — the platform's Hugging Face access token (super_admin).

  Lets this server download gated or private models (the speaker models). The token is
  write-only: the API never returns any part of it, so the input is never pre-filled and is
  cleared after a save. What the chip, the buttons and the notes show comes from the pure
  `hfTokenView` helper; this component renders it and calls the client. It is mounted on the
  Speaker Engine tab, which is itself super_admin-only (an admin sees that tab locked), so the
  card never renders for anyone whose calls would be refused.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import Spinner from '$components/ui/Spinner.svelte';
  import InfoTip from '$components/settings/transcription/InfoTip.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorStatus } from '$lib/utils/apiError';
  import {
    getHfTokenStatus,
    saveHfToken,
    removeHfToken,
    testHfToken,
    type HfTokenStatus,
    type HfTokenTestResult,
  } from '$lib/api/hfToken';
  import { hfTokenView, hfTokenTestKey, hfTokenSaveErrorKey } from '$lib/settings/hfToken';


  let status: HfTokenStatus | null = null;
  let loading = true;
  let loadFailed = false;
  let token = '';
  let saving = false;
  let testing = false;
  let removing = false;
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;
  let testResult: HfTokenTestResult | { ok: false; code: 'error' } | null = null;

  $: view = hfTokenView(status);
  $: busy = saving || testing || removing;
  $: typed = token.trim();
  $: canTest = !busy && (typed !== '' || view.canTestInEffect);

  onMount(loadStatus);

  async function loadStatus() {
    loading = true;
    loadFailed = false;
    try {
      status = await getHfTokenStatus();
    } catch {
      console.error('Failed to load the Hugging Face token status');
      status = null;
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function save() {
    if (!typed) return;
    saving = true;
    try {
      status = await saveHfToken(typed);
      token = '';
      testResult = null;
      toastStore.success($t('settings.engineSettings.hfToken.saved'));
    } catch (err) {
      console.error('Failed to save the Hugging Face token');
      toastStore.error($t(hfTokenSaveErrorKey(getErrorStatus(err))));
    } finally {
      saving = false;
    }
  }

  async function test() {
    testing = true;
    try {
      testResult = await testHfToken(typed !== '' ? typed : undefined);
    } catch {
      console.error('The Hugging Face token test could not run');
      testResult = { ok: false, code: 'error' };
    } finally {
      testing = false;
    }
  }

  function askRemove() {
    confirmDialog.request(
      $t('settings.engineSettings.hfToken.remove'),
      $t('settings.engineSettings.hfToken.removeConfirm'),
      remove,
      { confirmText: $t('settings.engineSettings.hfToken.remove'), variant: 'danger' }
    );
  }

  async function remove() {
    removing = true;
    try {
      status = await removeHfToken();
      token = '';
      testResult = null;
      toastStore.success($t('settings.engineSettings.hfToken.removed'));
    } catch {
      toastStore.error($t('settings.engineSettings.hfToken.removeFailed'));
    } finally {
      removing = false;
    }
  }
</script>

<section class="hf-token" data-testid="hf-token-card" aria-labelledby="hf-token-heading">
  <div class="head">
    <h3 id="hf-token-heading" class="title">{$t('settings.engineSettings.hfToken.heading')}</h3>
    <InfoTip inline label={$t('settings.engineSettings.hfToken.helpLabel')}>{$t('settings.engineSettings.hfToken.help')}</InfoTip>
    {#if !loading}
      <span class="settings-pill status-{view.badge}" data-testid="hf-token-status">{$t(view.badgeKey)}</span>
    {/if}
  </div>
  <p class="desc">{$t('settings.engineSettings.hfToken.description')}</p>

  {#if loading}
    <Spinner label={$t('common.loading')} size="small" />
  {:else}
    {#if loadFailed}
      <p class="note warn" role="alert" data-testid="hf-token-load-failed">
        {$t('settings.engineSettings.hfToken.loadFailed')}
      </p>
    {:else if view.noteKey}
      <p class="note" data-testid="hf-token-note">{$t(view.noteKey)}</p>
    {/if}

    <label for="hf-token-input" class="label">{$t('settings.engineSettings.hfToken.inputLabel')}</label>
    <div class="row">
      <input
        id="hf-token-input"
        type="password"
        autocomplete="off"
        spellcheck="false"
        dir="ltr"
        class="input"
        placeholder={$t('settings.engineSettings.hfToken.inputPlaceholder')}
        bind:value={token}
        disabled={busy}
      />
      <button
        type="button"
        class="btn btn-primary"
        on:click={save}
        disabled={busy || typed === ''}
      >
        {#if saving}<Spinner size="small" color="white" />{/if}
        {$t(view.saveLabelKey)}
      </button>
    </div>
    <p class="hint">{$t('settings.engineSettings.hfToken.inputHint')}</p>

    <div class="actions">
      <button type="button" class="btn btn-secondary" on:click={test} disabled={!canTest}>
        {testing ? $t('settings.engineSettings.hfToken.testing') : $t('settings.engineSettings.hfToken.test')}
      </button>
      {#if view.canRemove}
        <button
          type="button"
          class="btn btn-danger"
          on:click={askRemove}
          disabled={busy}
        >
          {$t('settings.engineSettings.hfToken.remove')}
        </button>
      {/if}
    </div>

    {#if testResult}
      <p class="result" class:ok={testResult.ok} role="status" data-testid="hf-token-test-result">
        {$t(hfTokenTestKey(testResult.code))}
      </p>
    {/if}
  {/if}
</section>

<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel}
  variant={$confirmVariant}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .hf-token {
    max-width: 800px;
    margin-top: var(--space-6);
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--surface-color);
  }

  .head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }

  .title {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .status-app,
  .status-environment {
    color: var(--success-color);
    border-color: var(--success-color);
  }

  .desc,
  .hint,
  .note {
    margin: var(--space-1-5) 0 var(--space-3);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .hint {
    margin: var(--space-1-5) 0 0;
    font-size: var(--font-size-xs);
  }

  .note.warn {
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    border: 1px solid var(--warning-color);
    color: var(--text-color);
  }

  .label {
    display: block;
    margin-bottom: var(--space-1-5);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .row {
    display: flex;
    gap: var(--space-2);
    align-items: stretch;
  }

  .input {
    flex: 1;
    min-width: 0;
  }

  .actions {
    display: flex;
    gap: var(--space-2);
    margin-top: var(--space-3);
    flex-wrap: wrap;
  }

  .result {
    margin: var(--space-2) 0 0;
    font-size: var(--font-size-sm);
    color: var(--error-color);
  }

  .result.ok {
    color: var(--success-color);
  }
</style>
