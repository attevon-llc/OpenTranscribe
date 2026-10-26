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
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import type { CollectionVisibility } from '$lib/types/collection';

  // Identity / wiring
  export let idPrefix: string;
  export let titleId: string;
  export let title: string;
  export let namePlaceholder: string;
  export let submitLabel: string;
  export let busyLabel: string;

  // Bound form values (two-way so parent state stays the source of truth)
  export let name: string;
  export let description: string;
  export let promptId: string | null;
  /** Who sees an organization collection. Only rendered when `showVisibility` is true. */
  export let visibility: CollectionVisibility = 'organization';
  /** True for an organization collection the caller may restrict (create, or `can_change_visibility`). */
  export let showVisibility = false;

  // State
  export let busy = false;
  export let loadingPrompts = false;
  export let availablePrompts: PromptOption[] = [];

  const dispatch = createEventDispatcher<{ submit: void; close: void }>();
</script>

<BaseModal isOpen={true} {title} {titleId} size="md" onClose={() => dispatch('close')}>
  <form id="{idPrefix}-form" on:submit|preventDefault={() => dispatch('submit')} class="config-form">
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

    {#if showVisibility}
      <fieldset class="form-group visibility-group" disabled={busy}>
        <legend>{$t('collectionsPanel.visibility.label')}</legend>
        <label class="visibility-option" class:chosen={visibility === 'organization'}>
          <input
            type="radio"
            name={`${idPrefix}-visibility`}
            value="organization"
            bind:group={visibility}
          />
          <span class="visibility-text">
            <span class="visibility-title">{$t('collectionsPanel.visibility.organization')}</span>
            <span class="form-hint">{$t('collectionsPanel.visibility.organizationHint')}</span>
          </span>
        </label>
        <label class="visibility-option" class:chosen={visibility === 'restricted'}>
          <input
            type="radio"
            name={`${idPrefix}-visibility`}
            value="restricted"
            bind:group={visibility}
          />
          <span class="visibility-text">
            <span class="visibility-title">
              <MenuIcon name="lock" size={14} />
              {$t('collectionsPanel.visibility.restricted')}
            </span>
            <span class="form-hint">{$t('collectionsPanel.visibility.restrictedHint')}</span>
          </span>
        </label>
      </fieldset>
    {/if}
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

  .visibility-group {
    border: 0;
    padding: 0;
    min-inline-size: 0;
  }

  .visibility-group legend {
    padding: 0;
    margin-bottom: 0.5rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .visibility-option {
    display: flex;
    gap: 0.6rem;
    align-items: flex-start;
    padding: 0.6rem 0.75rem;
    margin-bottom: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    cursor: pointer;
    font-weight: 400;
  }

  .visibility-option.chosen {
    border-color: var(--primary-color);
    background: var(--hover-color);
  }

  .visibility-option input {
    margin-top: 0.2rem;
  }

  .visibility-text {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .visibility-title {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
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
