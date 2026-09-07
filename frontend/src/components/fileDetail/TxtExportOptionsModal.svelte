<script lang="ts">
  import { createEventDispatcher } from 'svelte';
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

{#if show}
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:wheel|stopPropagation on:touchmove|stopPropagation>
    <div class="modal-dialog">
      <div class="modal-content">
        <div class="modal-header">
          <h2 class="modal-title">{$t('exportOptions.title')}</h2>
          <button
            class="modal-close-btn"
            on:click={close}
            aria-label={$t('modal.closeDialog')}
          >
            ×
          </button>
        </div>

        <div class="modal-body">
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
        </div>

        <div class="modal-footer">
          <button class="btn btn-primary" on:click={confirm}>
            {$t('exportOptions.export')}
          </button>
          <button class="btn btn-cancel" on:click={close}>
            {$t('common.cancel')}
          </button>
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: var(--z-modal);
    padding: 1rem;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .modal-dialog {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    max-width: 500px;
    width: 100%;
    overflow: hidden;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
    animation: slideIn 0.2s ease-out;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-title {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-color);
    line-height: 1.4;
  }

  .modal-close-btn {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.5rem;
    color: var(--text-secondary);
    transition: color 0.2s ease;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.5rem;
    line-height: 1;
  }

  .modal-close-btn:hover {
    color: var(--text-color);
    background: var(--button-hover);
  }

  .modal-body {
    padding: 1.5rem;
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
    margin-right: 0.3rem;
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
    color: var(--text-secondary-color);
    font-style: italic;
  }

  .modal-footer {
    display: flex;
    gap: 0.75rem;
    padding: 1rem 1.5rem 1.5rem;
    justify-content: flex-end;
    border-top: 1px solid var(--border-color);
    flex-wrap: wrap;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border: none;
    border-radius: 10px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    min-width: 120px;
  }

  .btn-primary {
    background: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .btn-primary:hover {
    background: #2563eb;
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .btn-primary:active {
    transform: translateY(0);
  }

  .btn-cancel {
    background: var(--card-background);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    box-shadow: var(--card-shadow);
  }

  .btn-cancel:hover {
    background: var(--button-hover);
    border-color: var(--primary-color);
    transform: translateY(-1px);
  }

  /* Responsive design */
  @media (max-width: 480px) {
    .modal-dialog {
      margin: 1rem;
      max-width: none;
    }

    .modal-footer {
      flex-direction: column-reverse;
    }

    .btn {
      width: 100%;
    }
  }

  /* Dark mode adjustments */
  :global([data-theme='dark']) .modal-overlay {
    background: rgba(0, 0, 0, 0.7);
  }

  :global([data-theme='dark']) .modal-dialog {
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3), 0 10px 10px -5px rgba(0, 0, 0, 0.2);
  }
</style>
