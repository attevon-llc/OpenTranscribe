<script context="module" lang="ts">
  export type PromptOption = {
    uuid: string;
    name: string;
    is_system_default?: boolean;
    [key: string]: any;
  };
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';

  // Identity / wiring
  export let idPrefix: string;
  export let titleId: string;
  export let title: string;
  export let intro: string;
  export let namePlaceholder: string;
  export let submitLabel: string;
  export let busyLabel: string;

  // Bound form values (two-way so parent state stays the source of truth)
  export let name: string;
  export let description: string;
  export let promptId: string | null;

  // State
  export let busy = false;
  export let loadingPrompts = false;
  export let availablePrompts: PromptOption[] = [];

  const dispatch = createEventDispatcher<{ submit: void; close: void }>();
</script>

<BaseModal isOpen={true} {title} {titleId} size="md" onClose={() => dispatch('close')}>
  <form id="{idPrefix}-form" on:submit|preventDefault={() => dispatch('submit')} class="config-form">
    <p class="modal-intro">{intro}</p>

    <div class="form-group">
      <label for={`${idPrefix}-name`}>{$t('collectionsPanel.name')}</label>
      <input
        id={`${idPrefix}-name`}
        type="text"
        bind:value={name}
        placeholder={namePlaceholder}
        maxlength="100"
        required
        disabled={busy}
      />
      <span class="form-hint">{$t('collectionsPanel.nameHint')}</span>
    </div>

    <div class="form-group">
      <label for={`${idPrefix}-description`}>{$t('collectionsPanel.description')}</label>
      <textarea
        id={`${idPrefix}-description`}
        bind:value={description}
        placeholder={$t('collectionsPanel.descriptionPlaceholder')}
        rows="3"
        maxlength="500"
        disabled={busy}
      ></textarea>
      <span class="form-hint">{$t('collectionsPanel.descriptionHint')}</span>
    </div>

    <div class="form-group">
      <label for={`${idPrefix}-prompt`}>{$t('collectionsPanel.defaultPrompt')}</label>
      <select
        id={`${idPrefix}-prompt`}
        bind:value={promptId}
        disabled={busy || loadingPrompts}
      >
        <option value={null}>{$t('collectionsPanel.noDefaultPrompt')}</option>
        {#each availablePrompts as prompt}
          <option value={prompt.uuid}>
            {prompt.name}{prompt.is_system_default ? ` (${$t('collectionsPanel.system')})` : ''}
          </option>
        {/each}
      </select>
      <span class="form-hint">{$t('collectionsPanel.defaultPromptHint')}</span>
    </div>
  </form>

  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={() => dispatch('close')} disabled={busy}>
      {$t('collectionsPanel.cancel')}
    </button>
    <button
      type="submit"
      form="{idPrefix}-form"
      class="btn btn-primary"
      disabled={busy || !name.trim()}
    >
      {#if busy}
        <Spinner size="small" />
        {busyLabel}
      {:else}
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
          <polyline points="17,21 17,13 7,13 7,21"/>
          <polyline points="7,3 7,8 15,8"/>
        </svg>
        {submitLabel}
      {/if}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .config-form {
    margin: 0;
  }

  .modal-intro {
    margin: 0 0 1.25rem 0;
    padding: 0.75rem 0.875rem;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
    background: rgba(var(--primary-color-rgb), 0.06);
    border: 1px solid rgba(var(--primary-color-rgb), 0.15);
    border-inline-start: 3px solid var(--primary-color);
    border-radius: 6px;
  }

  .form-group {
    margin-bottom: 1.25rem;
  }

  .form-group:last-child {
    margin-bottom: 0;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.5rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .form-hint {
    display: block;
    margin-top: 0.35rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
  }
</style>
