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
  let showRemoveConfirm = false;
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
      <span class="status-chip status-{view.badge}" data-testid="hf-token-status">{$t(view.badgeKey)}</span>
    {/if}
  </div>
  <p class="desc">{$t('settings.engineSettings.hfToken.description')}</p>

  {#if loading}
    <Spinner size="small" />
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
          on:click={() => (showRemoveConfirm = true)}
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

{#if showRemoveConfirm}
  <ConfirmationModal
    bind:isOpen={showRemoveConfirm}
    title={$t('settings.engineSettings.hfToken.remove')}
    message={$t('settings.engineSettings.hfToken.removeConfirm')}
    confirmText={$t('settings.engineSettings.hfToken.remove')}
    cancelText={$t('common.cancel')}
    confirmButtonClass="modal-delete-button"
    cancelButtonClass="modal-cancel-button"
    on:confirm={remove}
  />
{/if}

<style>
  .hf-token {
    max-width: 800px;
    margin-top: 1.5rem;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .title {
    margin: 0;
    font-size: var(--font-size-md, 0.95rem);
    font-weight: var(--font-weight-semibold, 600);
    color: var(--text-color);
  }

  .status-chip {
    margin-inline-start: auto;
    padding: 0.125rem 0.625rem;
    border-radius: 999px;
    border: 1px solid var(--border-color);
    font-size: 0.75rem;
    font-weight: var(--font-weight-medium, 500);
    color: var(--text-secondary);
  }

  .status-app,
  .status-environment {
    color: var(--success-color);
    border-color: var(--success-color);
  }

  .desc,
  .hint,
  .note {
    margin: 0.375rem 0 0.75rem;
    font-size: 0.8125rem;
    color: var(--text-muted);
  }

  .hint {
    margin: 0.375rem 0 0;
    font-size: 0.75rem;
  }

  .note.warn {
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    border: 1px solid var(--warning-color);
    color: var(--text-color);
  }

  .label {
    display: block;
    margin-bottom: 0.375rem;
    font-size: 0.8125rem;
    font-weight: var(--font-weight-medium, 500);
    color: var(--text-color);
  }

  .row {
    display: flex;
    gap: 0.5rem;
    align-items: stretch;
  }

  .input {
    flex: 1;
    min-width: 0;
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.75rem;
    flex-wrap: wrap;
  }

  .result {
    margin: 0.5rem 0 0;
    font-size: 0.8125rem;
    color: var(--error-color);
  }

  .result.ok {
    color: var(--success-color);
  }
</style>
