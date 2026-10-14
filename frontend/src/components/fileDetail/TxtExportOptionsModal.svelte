<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { t } from '$stores/locale';

  // Visibility (two-way bound by the page; the page owns the scroll-lock sequencing)
  export let show = false;

  // TXT export option values (two-way bound to the page's txtExportOptions object)
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

<BaseModal isOpen={show} title={$t('exportOptions.title')} size="sm" onClose={close}>
  <p class="modal-message">{$t('exportOptions.description')}</p>
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
  </div>
  {#if redactionActive && !showOriginal}
    <p class="export-redaction-note">
      <svg class="export-redaction-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
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
    gap: 8px;
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
    gap: 0.75rem;
    margin-top: 1rem;
  }

  .export-redaction-note {
    margin-top: 1rem;
    padding: 0.6rem 0.75rem;
    background: rgba(var(--warning-color-rgb, 234, 179, 8), 0.12);
    border-radius: 6px;
    font-size: 0.85rem;
    color: var(--text-color);
  }
  .export-redaction-icon {
    vertical-align: -2px;
    margin-inline-end: 0.3rem;
    color: var(--warning-color);
  }
  .export-redaction-hint {
    display: block;
    margin-top: 0.25rem;
    color: var(--text-secondary);
  }

  .export-option-label {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    cursor: pointer;
    font-size: 0.95rem;
    color: var(--text-color);
  }

  .export-option-label input[type="checkbox"] {
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
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
  }
</style>
