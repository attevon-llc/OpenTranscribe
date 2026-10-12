<!--
  PyannoteCredentialForm.svelte — the user's pyannote.ai API key (issue #1204).

  The key is write-only: the API never returns it, so the input is never pre-filled and is
  cleared after a save. Which buttons are usable, the badge and the warning all come from the
  pure `pyannoteCredentialView` helper; this component only renders them and calls the client.
  Rendered under the speaker-detection source select, and only when the deployment lets users
  bring their own provider keys (`asr.user_providers`), so a locked or absent capability never
  shows a form whose routes would 404.
-->
<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorStatus } from '$lib/utils/apiError';
  import {
    getPyannoteCredential,
    savePyannoteCredential,
    deletePyannoteCredential,
    testPyannoteCredential,
    type PyannoteCredentialStatus,
    type PyannoteTestResult,
  } from '$lib/api/pyannoteCredential';
  import {
    pyannoteCredentialView,
    pyannoteDeletedKey,
    pyannoteSaveErrorKey,
    pyannoteTestResultKey,
  } from '$lib/settings/pyannoteCredential';

  /** The user's current (possibly unsaved) speaker-detection source. */
  export let diarizationSource = 'provider';
  /** Disable every control, e.g. while the parent form is saving. */
  export let disabled = false;

  const dispatch = createEventDispatcher<{
    saved: void;
    deleted: { sourceReverted: boolean };
    tested: PyannoteTestResult;
  }>();

  let status: PyannoteCredentialStatus | null = null;
  let loading = true;
  let apiKey = '';
  let saving = false;
  let testing = false;
  let deleting = false;
  let showDeleteConfirm = false;
  let testResult: PyannoteTestResult | null = null;

  $: view = pyannoteCredentialView(status, diarizationSource);
  $: busy = saving || testing || deleting;
  $: trimmedKey = apiKey.trim();
  // A typed key is tested before it is saved; with an empty box the saved key is tested.
  $: canTest = !disabled && !busy && (trimmedKey !== '' ? view.canSave : view.canTestSaved);

  onMount(loadStatus);

  async function loadStatus() {
    loading = true;
    try {
      status = await getPyannoteCredential();
    } catch (err) {
      console.error('Failed to load the pyannote.ai key status:', err);
      status = null;
    } finally {
      loading = false;
    }
  }

  async function save() {
    if (!trimmedKey) return;
    saving = true;
    try {
      status = await savePyannoteCredential(trimmedKey);
      apiKey = '';
      testResult = null;
      toastStore.success($t('settings.speakerIdentification.pyannoteKey.saved'));
      dispatch('saved');
    } catch (err) {
      console.error('Failed to save the pyannote.ai key');
      toastStore.error($t(pyannoteSaveErrorKey(getErrorStatus(err))));
    } finally {
      saving = false;
    }
  }

  async function test() {
    testing = true;
    try {
      const typed = trimmedKey !== '';
      const result = await testPyannoteCredential(typed ? trimmedKey : undefined);
      testResult = result;
      dispatch('tested', result);
      // Testing the saved key stores the outcome server-side, so the badge needs a refresh.
      if (!typed) await loadStatus();
    } catch (err) {
      console.error('The pyannote.ai connection test could not run');
      testResult = { success: false, code: 'error', message: '', response_time_ms: 0 };
      toastStore.error($t(pyannoteTestResultKey('error')));
    } finally {
      testing = false;
    }
  }

  async function remove() {
    deleting = true;
    try {
      const result = await deletePyannoteCredential();
      testResult = null;
      apiKey = '';
      await loadStatus();
      toastStore.success($t(pyannoteDeletedKey(result.source_reverted)));
      dispatch('deleted', { sourceReverted: result.source_reverted });
    } catch {
      toastStore.error($t('settings.speakerIdentification.pyannoteKey.deleteFailed'));
    } finally {
      deleting = false;
    }
  }
</script>

<div class="pyannote-credential" data-testid="pyannote-credential-form">
  <div class="credential-head">
    <h4 class="credential-title">{$t('settings.speakerIdentification.pyannoteKey.heading')}</h4>
    {#if !loading}
      <span class="credential-badge badge-{view.badge}" data-testid="pyannote-badge">
        {$t(view.badgeKey)}
      </span>
    {/if}
  </div>
  <p class="credential-desc">{$t('settings.speakerIdentification.pyannoteKey.description')}</p>

  {#if loading}
    <Spinner size="small" />
  {:else}
    {#if view.warningKey}
      <p class="credential-warning" role="alert" data-testid="pyannote-warning">
        {$t(view.warningKey)}
      </p>
    {/if}

    <label for="pyannote-api-key" class="credential-label">
      {$t('settings.speakerIdentification.pyannoteKey.inputLabel')}
    </label>
    <div class="credential-row">
      <input
        id="pyannote-api-key"
        type="password"
        autocomplete="off"
        spellcheck="false"
        class="credential-input"
        placeholder={$t('settings.speakerIdentification.pyannoteKey.inputPlaceholder')}
        bind:value={apiKey}
        disabled={disabled || busy || !view.canSave}
      />
      <button
        type="button"
        class="cred-btn primary"
        on:click={save}
        disabled={disabled || busy || !view.canSave || trimmedKey === ''}
      >
        {#if saving}<Spinner size="small" color="white" />{/if}
        {$t(view.saveLabelKey)}
      </button>
    </div>

    <div class="credential-actions">
      <button type="button" class="cred-btn" on:click={test} disabled={!canTest}>
        {testing
          ? $t('settings.speakerIdentification.pyannoteKey.testing')
          : $t('settings.speakerIdentification.pyannoteKey.test')}
      </button>
      {#if view.canDelete}
        <button
          type="button"
          class="cred-btn danger"
          on:click={() => (showDeleteConfirm = true)}
          disabled={disabled || busy}
        >
          {$t('settings.speakerIdentification.pyannoteKey.delete')}
        </button>
      {/if}
    </div>
    <p class="credential-note">{$t('settings.speakerIdentification.pyannoteKey.testNote')}</p>

    {#if testResult}
      <p
        class="credential-result"
        class:ok={testResult.success}
        role="status"
        data-testid="pyannote-test-result"
      >
        {$t(pyannoteTestResultKey(testResult.code))}
      </p>
    {/if}
  {/if}
</div>

{#if showDeleteConfirm}
  <ConfirmationModal
    bind:isOpen={showDeleteConfirm}
    title={$t('settings.speakerIdentification.pyannoteKey.delete')}
    message={$t('settings.speakerIdentification.pyannoteKey.deleteConfirm')}
    confirmText={$t('settings.speakerIdentification.pyannoteKey.delete')}
    cancelText={$t('common.cancel')}
    confirmButtonClass="modal-delete-button"
    cancelButtonClass="modal-cancel-button"
    on:confirm={remove}
  />
{/if}

<style>
  .pyannote-credential {
    margin-top: 1rem;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .credential-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .credential-title {
    margin: 0;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .credential-badge {
    padding: 0.125rem 0.625rem;
    border-radius: 999px;
    border: 1px solid var(--border-color);
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-secondary);
  }

  .credential-badge.badge-verified,
  .credential-badge.badge-configured {
    color: var(--success-color);
    border-color: var(--success-color);
  }

  .credential-badge.badge-failed {
    color: var(--error-color);
    border-color: var(--error-color);
  }

  .credential-desc,
  .credential-note {
    margin: 0.375rem 0 0.75rem;
    font-size: 0.8125rem;
    color: var(--text-muted);
  }

  .credential-note {
    margin: 0.5rem 0 0;
    font-size: 0.75rem;
  }

  .credential-warning {
    margin: 0 0 0.75rem;
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    border: 1px solid var(--warning-color);
    font-size: 0.8125rem;
    color: var(--text-color);
  }

  .credential-label {
    display: block;
    margin-bottom: 0.375rem;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .credential-row {
    display: flex;
    gap: 0.5rem;
    align-items: stretch;
  }

  .credential-input {
    flex: 1;
    min-width: 0;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--background-color);
    color: var(--text-color);
    font-size: 0.875rem;
  }

  .credential-input:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(var(--primary-color-rgb), 0.1);
  }

  .credential-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.75rem;
    flex-wrap: wrap;
  }

  /* Own button classes: the enclosing form's tests and e2e select its Save and Reset
     buttons by .btn-primary / .btn-secondary, so these must not collide with them. */
  .cred-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.5rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--background-color);
    color: var(--text-color);
    font-size: 0.875rem;
    font-weight: 500;
    cursor: pointer;
  }

  .cred-btn.primary {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: white;
  }

  .cred-btn.danger {
    color: var(--error-color);
    border-color: var(--error-color);
  }

  .cred-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .credential-result {
    margin: 0.5rem 0 0;
    font-size: 0.8125rem;
    color: var(--error-color);
  }

  .credential-result.ok {
    color: var(--success-color);
  }
</style>
