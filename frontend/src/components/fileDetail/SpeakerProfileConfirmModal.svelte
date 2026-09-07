<script lang="ts">
  /**
   * Confirmation dialog asking whether a renamed speaker should update its
   * global profile or become a new one.
   *
   * The page owns visibility (and the scroll lock keyed off it), so this
   * component is rendered inside the page's `{#if}` and only reports the
   * chosen action.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let title = '';
  export let message = '';

  const dispatch = createEventDispatcher();
</script>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="modal-overlay" on:wheel|stopPropagation on:touchmove|stopPropagation>
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h2 class="modal-title">{title}</h2>
        <button
          class="modal-close-btn"
          on:click={() => dispatch('cancel')}
          aria-label={$t('modal.closeDialog')}
        >
          ×
        </button>
      </div>

      <div class="modal-body">
        <p class="modal-message">{message}</p>
      </div>

      <div class="modal-footer">
        <button
          class="btn btn-primary"
          on:click={() => dispatch('updateProfile')}
        >
          {$t('speakerProfile.updateGlobally')}
        </button>
        <button
          class="btn btn-secondary"
          on:click={() => dispatch('createNewProfile')}
        >
          {$t('speakerProfile.createNew')}
        </button>
        <button
          class="btn btn-cancel"
          on:click={() => dispatch('cancel')}
        >
          {$t('common.cancel')}
        </button>
      </div>
    </div>
  </div>
</div>

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

  .btn-secondary {
    background: var(--success-color);
    color: white;
    box-shadow: 0 2px 4px rgba(16, 185, 129, 0.2);
  }

  .btn-secondary:hover {
    background: #059669; /* Darker green to match app pattern */
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(16, 185, 129, 0.3);
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
