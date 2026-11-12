<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { t } from '$stores/locale';

  // Visibility (two-way bound by the page)
  export let show = false;
  export let title: string;
  export let description: string;

  // Option values (two-way bound to the page's options object)
  export let includeTimestamps = true;
  export let includeSpeakers = true;
  export let includeComments = false;
  export let hasComments = false;

  // Read-only display context
  export let diarizationDisabled = false;
  export let redactionActive = false;
  export let showOriginal = false;
  export let canViewOriginal = false;

  const dispatch = createEventDispatcher();

  function close() {
    show = false;
    dispatch('close');
  }

  function confirm() {
    dispatch('confirm');
  }
</script>

<BaseModal isOpen={show} {title} size="sm" onClose={close}>
  <p class="modal-message">{description}</p>
  <div class="export-options-list">
    <label class="export-option-label">
      <input type="checkbox" bind:checked={includeTimestamps} />
      {$t('exportOptions.includeTimestamps')}
    </label>
    <label class="export-option-label" class:disabled-option={diarizationDisabled}>
      <input type="checkbox" bind:checked={includeSpeakers} disabled={diarizationDisabled} />
      {$t('exportOptions.includeSpeakers')}
      {#if diarizationDisabled}
        <span class="option-hint">{$t('exportOptions.diarizationDisabledHint')}</span>
      {/if}
    </label>
    {#if hasComments}
      <label class="export-option-label">
        <input type="checkbox" bind:checked={includeComments} />
        {$t('exportOptions.includeComments')}
      </label>
    {/if}
    <slot name="fields" />
  </div>

  <slot name="note" />

  {#if redactionActive && !showOriginal}
    <p class="export-redaction-note">
      <svg class="export-redaction-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      {$t('settings.contentRedaction.exportRedactedNote')}
      {#if canViewOriginal}
        <span class="export-redaction-hint">
          {$t('settings.contentRedaction.exportOriginalHint')}
        </span>
      {/if}
    </p>
  {/if}

  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={close}>
      {$t('common.cancel')}
    </button>
    <button type="button" class="btn btn-primary export-confirm" on:click={confirm}>
      <MenuIcon name="download" size={16} />
      {$t('exportOptions.export')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .export-confirm {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  .modal-message {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.5;
    font-size: 0.95rem;
  }

  .export-options-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-top: var(--space-4);
  }

  .export-option-label {
    display: flex;
    align-items: center;
    gap: var(--space-2-5);
    cursor: pointer;
    font-size: 0.95rem;
    color: var(--text-color);
  }

  .export-option-label input[type='checkbox'] {
    width: 1rem;
    height: 1rem;
    cursor: pointer;
    accent-color: var(--primary-color);
  }

  .export-option-label.disabled-option {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .option-hint {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    font-style: italic;
  }

  .export-redaction-note {
    margin-top: var(--space-4);
    padding: var(--space-2-5) var(--space-3);
    background: rgba(var(--warning-color-rgb), 0.12);
    border-radius: var(--radius-md);
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .export-redaction-icon {
    vertical-align: -2px;
    margin-inline-end: var(--space-1);
    color: var(--warning-text);
  }

  .export-redaction-hint {
    display: block;
    margin-top: var(--space-1);
    color: var(--text-secondary);
  }
</style>
