<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from './ui/BaseModal.svelte';

  export let isOpen = false;
  export let title = '';
  export let message = '';
  export let confirmText = '';
  export let cancelText = '';
  export let confirmButtonClass = 'confirm-button';
  export let cancelButtonClass = 'cancel-button';

  // Apply defaults from translations if not provided
  $: resolvedTitle = title || $t('modal.confirmAction');
  $: resolvedMessage = message || $t('modal.confirmMessage');
  $: resolvedConfirmText = confirmText || $t('modal.confirm');
  $: resolvedCancelText = cancelText || $t('modal.cancel');

  const dispatch = createEventDispatcher();

  function handleConfirm() {
    dispatch('confirm');
    isOpen = false;
  }

  function handleCancel() {
    dispatch('cancel');
    isOpen = false;
  }

  function handleClose() {
    dispatch('close');
    isOpen = false;
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      // Don't intercept Enter when user is typing in an input/textarea
      const target = event.target as HTMLElement;
      if (target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA' || target?.tagName === 'SELECT') {
        return;
      }
      handleConfirm();
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<BaseModal {isOpen} title={resolvedTitle} onClose={handleClose} maxWidth="500px" zIndex={1300}>
  <p id="modal-message" class="modal-message">{resolvedMessage}</p>

  <svelte:fragment slot="footer">
    {#if resolvedCancelText}
      <button
        class="modal-button {cancelButtonClass}"
        on:click={handleCancel}
        type="button"
      >
        {resolvedCancelText}
      </button>
    {/if}
    <button
      class="modal-button {confirmButtonClass}"
      on:click={handleConfirm}
      type="button"
    >
      {resolvedConfirmText}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .modal-message {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.5;
    font-size: 0.95rem;
  }

  .modal-button {
    padding: 0.6rem 1.2rem;
    border: none;
    border-radius: 8px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    min-width: 120px;
  }

  .cancel-button {
    background: var(--card-background);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    padding: 0.6rem 1.2rem;
    border-radius: 8px;
    font-size: 0.95rem;
    font-weight: 500;
    transition: all 0.2s ease;
    cursor: pointer;
    box-shadow: var(--card-shadow);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    min-width: 120px;
    /* Ensure high contrast */
    opacity: 1;
  }

  .cancel-button:hover {
    background: var(--button-hover, #e5e7eb);
    border-color: var(--border-color);
  }

  .confirm-button {
    background: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .confirm-button:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.3);
  }

  .confirm-button:active {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  /* Delete / destructive confirmation — red override */
  .delete-confirm {
    background: #ef4444;
    color: white;
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .delete-confirm:hover {
    background: #dc2626;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.3);
  }

  .delete-confirm:active {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  /* Dark mode adjustments */
  :global([data-theme='dark']) .cancel-button:hover {
    background: var(--button-hover, rgba(255, 255, 255, 0.1));
    border-color: var(--border-color);
  }

  /* Responsive design */
  @media (max-width: 768px) {
    .modal-button {
      width: 100%;
      min-height: 44px;
    }
  }

  /* Focus styles for accessibility */
  .modal-button:focus {
    outline: 2px solid #3b82f6;
    outline-offset: 2px;
  }

  .cancel-button:focus {
    outline: 2px solid var(--text-color);
    outline-offset: 2px;
  }

  /* Reduced motion support */
  @media (prefers-reduced-motion: reduce) {
    .modal-button {
      transition: none;
    }
  }
</style>
