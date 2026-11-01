<!--
  LegalProcessingGateCard.svelte — "require acceptance before processing" (super_admin).

  Which served legal documents a file's OWNER must have accepted before anything processes the
  file, and whose acceptance counts. The server validates every key and enforces the rule at
  every entry point (app.legal.processing_gate); this card only edits the two settings.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import {
    getProcessingGateSettings,
    updateProcessingGateSettings,
    type ProcessingAcceptanceScope,
    type ProcessingGateSettings,
  } from '$lib/api/legal';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  let settings: ProcessingGateSettings | null = null;
  let loading = true;
  let loadFailed = false;
  let saving = false;
  let selected: string[] = [];
  let scope: ProcessingAcceptanceScope = 'either';

  const SCOPES: { value: ProcessingAcceptanceScope; key: string }[] = [
    { value: 'user', key: 'legal.processingGate.scopeUser' },
    { value: 'organization', key: 'legal.processingGate.scopeOrganization' },
    { value: 'either', key: 'legal.processingGate.scopeEither' },
  ];

  function apply(next: ProcessingGateSettings) {
    settings = next;
    selected = [...next.required_document_keys];
    scope = next.acceptance_scope;
  }

  $: dirty =
    !!settings &&
    (scope !== settings.acceptance_scope ||
      selected.length !== settings.required_document_keys.length ||
      selected.some((k) => !settings?.required_document_keys.includes(k)));

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      apply(await getProcessingGateSettings());
    } catch (error) {
      console.error('Failed to load the processing requirement:', error);
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function save() {
    if (!settings || saving) return;
    saving = true;
    try {
      apply(
        await updateProcessingGateSettings({
          required_document_keys: selected,
          acceptance_scope: scope,
        })
      );
      toastStore.success($t('legal.processingGate.saved'));
    } catch (error) {
      console.error('Failed to save the processing requirement:', error);
      toastStore.error(getErrorMessage(error, $t('legal.processingGate.saveError')));
    } finally {
      saving = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<section class="gate" data-testid="legal-processing-gate">
  <h4 class="gate-title">{$t('legal.processingGate.title')}</h4>
  <p class="gate-note">{$t('legal.processingGate.description')}</p>

  {#if loading}
    <SettingsSkeleton variant="rows" count={2} label={$t('common.loading')} />
  {:else if loadFailed || !settings}
    <p class="gate-error" role="alert">{$t('legal.processingGate.loadError')}</p>
    <button type="button" class="btn btn-secondary" on:click={load}>
      {$t('settings.modal.sectionRetry')}
    </button>
  {:else}
    <fieldset class="gate-group">
      <legend>{$t('legal.processingGate.documentsLabel')}</legend>
      {#each settings.available_documents as doc (doc.key)}
        <label class="gate-option">
          <input
            type="checkbox"
            value={doc.key}
            bind:group={selected}
            data-testid="legal-gate-doc"
          />
          <span dir="auto">{doc.title}</span>
        </label>
      {/each}
    </fieldset>

    <fieldset class="gate-group">
      <legend>{$t('legal.processingGate.scopeLabel')}</legend>
      {#each SCOPES as option (option.value)}
        <label class="gate-option">
          <input type="radio" name="legal-gate-scope" value={option.value} bind:group={scope} />
          <span>{$t(option.key)}</span>
        </label>
      {/each}
    </fieldset>

    {#if !settings.enabled}
      <p class="gate-note" data-testid="legal-gate-off">{$t('legal.processingGate.statusOff')}</p>
    {/if}

    <div class="gate-actions">
      <button
        type="button"
        class="btn btn-primary"
        disabled={!dirty || saving}
        on:click={save}
        data-testid="legal-gate-save"
      >
        {$t('legal.processingGate.save')}
      </button>
    </div>
  {/if}
</section>

<style>
  .gate {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-block: 8px 20px;
    padding: 12px 14px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--bg-secondary);
  }

  .gate-title {
    margin: 0;
  }

  .gate-note {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    max-width: var(--measure-prose, 72ch);
  }

  .gate-group {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin: 0;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .gate-group legend {
    padding: 0 0.375rem;
    font-weight: var(--font-weight-medium, 500);
  }

  .gate-option {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    cursor: pointer;
  }

  .gate-actions {
    display: flex;
    justify-content: flex-end;
  }

  .gate-error {
    margin: 0;
    color: var(--error-color, #dc2626);
  }
</style>
