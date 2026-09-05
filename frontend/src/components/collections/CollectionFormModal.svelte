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
  import { fade, slide } from 'svelte/transition';
  import { t } from '$stores/locale';
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

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div
  class="modal-backdrop"
  role="presentation"
  on:wheel|preventDefault|self
  on:touchmove|preventDefault|self
  on:keydown={(e) => e.key === 'Escape' && dispatch('close')}
  transition:fade
>
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <div
    class="modal-container"
    role="dialog"
    aria-modal="true"
    aria-labelledby={titleId}
    on:click|stopPropagation
    on:keydown|stopPropagation
    transition:slide
  >
    <div class="modal-content">
      <div class="modal-header">
        <h2 id={titleId}>{title}</h2>
        <button
          class="modal-close-button"
          on:click={() => dispatch('close')}
          type="button"
          aria-label={$t('collectionsPanel.closeModal')}
          title={$t('collectionsPanel.closeModal')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>

      <form on:submit|preventDefault={() => dispatch('submit')} class="config-form">
        <p class="modal-intro">{intro}</p>

        <div class="form-group">
          <label for={`${idPrefix}-name`}>{$t('collectionsPanel.name')}</label>
          <input
            id={`${idPrefix}-name`}
            type="text"
            bind:value={name}
            class="form-control"
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
            class="form-control"
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
            class="form-control"
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

        <div class="form-actions">
          <button
            type="button"
            class="cancel-button"
            on:click={() => dispatch('close')}
            disabled={busy}
          >
            {$t('collectionsPanel.cancel')}
          </button>
          <button
            type="submit"
            class="save-button primary"
            disabled={busy || !name.trim()}
          >
            {#if busy}
              <Spinner size="small" />
              {busyLabel}
            {:else}
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
                <polyline points="17,21 17,13 7,13 7,21"/>
                <polyline points="7,3 7,8 15,8"/>
              </svg>
              {submitLabel}
            {/if}
          </button>
        </div>
      </form>
    </div>
  </div>
</div>

<style>
  /* Modal styles - standard pattern */
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--modal-backdrop, rgba(0, 0, 0, 0.5));
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: var(--z-modal);
    padding: 1rem;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .modal-container {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    max-width: 600px;
    width: 100%;
    max-height: 90vh;
    overflow-y: auto;
    overscroll-behavior: contain;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
    animation: slideIn 0.2s ease-out;
  }

  .modal-content {
    display: flex;
    flex-direction: column;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.125rem;
    font-weight: 600;
    color: var(--text-color);
    line-height: 1.4;
  }

  .modal-close-button {
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
  }

  .modal-close-button:hover {
    color: var(--text-color);
    background: var(--button-hover);
  }

  .config-form {
    padding: 1.5rem;
  }

  .modal-intro {
    margin: 0 0 1.25rem 0;
    padding: 0.75rem 0.875rem;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
    background: rgba(59, 130, 246, 0.06);
    border: 1px solid rgba(59, 130, 246, 0.15);
    border-left: 3px solid var(--primary-color, #3b82f6);
    border-radius: 6px;
  }

  :global([data-theme='dark']) .modal-intro {
    background: rgba(59, 130, 246, 0.08);
    border-color: rgba(59, 130, 246, 0.2);
    border-left-color: var(--primary-color, #3b82f6);
  }

  .form-group {
    margin-bottom: 1.5rem;
  }

  .form-group label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .form-control {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background-color: var(--input-bg);
    color: var(--text-color);
    font-size: 0.875rem;
    transition: border-color 0.2s ease;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
  }

  .form-hint {
    display: block;
    margin-top: 0.35rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    gap: 1rem;
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
  }

  .cancel-button {
    background: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    padding: 0.6rem 1.2rem;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.95rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .cancel-button:hover:not(:disabled) {
    background: var(--button-hover, #e5e7eb);
    border-color: var(--border-color);
  }

  .save-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.95rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .save-button:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .save-button:active:not(:disabled) {
    transform: scale(1);
  }

  .save-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Dark mode adjustments */
  :global([data-theme='dark']) .modal-backdrop {
    background: rgba(0, 0, 0, 0.7);
  }

  :global([data-theme='dark']) .modal-container {
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3), 0 10px 10px -5px rgba(0, 0, 0, 0.2);
  }

  @media (prefers-reduced-motion: reduce) {
    .modal-container {
      animation: none;
    }
  }

  :global([data-theme='dark']) .form-group input,
  :global([data-theme='dark']) .form-group textarea {
    background: rgba(255, 255, 255, 0.03);
    color: var(--text-primary);
    border-color: var(--border-color);
  }

  :global([data-theme='dark']) .form-group input:focus,
  :global([data-theme='dark']) .form-group textarea:focus {
    background: rgba(255, 255, 255, 0.05);
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
  }

  :global([data-theme='dark']) .form-group input:hover,
  :global([data-theme='dark']) .form-group textarea:hover {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--primary-color);
  }

  /* Mobile: fullscreen modals and tap-friendly buttons */
  @media (max-width: 768px) {
    .modal-backdrop {
      align-items: stretch;
      padding: 0;
    }

    .modal-container {
      width: 100%;
      max-width: 100% !important;
      max-height: 100%;
      max-height: 100dvh;
      border-radius: 0;
      margin: 0;
    }

    .modal-header {
      padding: 1rem;
      padding-top: calc(1rem + env(safe-area-inset-top, 0px));
    }

    .modal-header h2 {
      font-size: 1.1rem;
    }

    .config-form {
      padding: 1rem;
    }

    .form-actions {
      flex-direction: column;
      gap: 0.75rem;
      padding-bottom: calc(1rem + env(safe-area-inset-bottom, 0px));
    }

    .cancel-button,
    .save-button {
      width: 100%;
      min-height: 44px;
      justify-content: center;
    }

    .modal-close-button {
      min-width: 44px;
      min-height: 44px;
    }
  }
</style>
