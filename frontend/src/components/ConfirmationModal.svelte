<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from './ui/BaseModal.svelte';

  export let isOpen = false;
  export let title = '';
  export let message = '';
  export let confirmText = '';
  export let cancelText = '';
  /** Confirm button colour. `danger` also makes the dialog an `alertdialog` focused on Cancel. */
  export let variant: 'primary' | 'danger' | 'warning' = 'primary';

  // Apply defaults from translations if not provided
  $: resolvedTitle = title || $t('modal.confirmAction');
  $: resolvedMessage = message || $t('modal.confirmMessage');
  $: resolvedConfirmText = confirmText || $t('modal.confirm');
  $: resolvedCancelText = cancelText || $t('modal.cancel');
  $: destructive = variant === 'danger';

  const dispatch = createEventDispatcher();

  // Close before dispatching: a confirm handler may open the next dialog on the same bound
  // store (a two-step confirmation), and closing afterwards would shut that one too.
  function handleConfirm() {
    isOpen = false;
    dispatch('confirm');
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
    // The listener is on window and the component stays mounted while closed: without this guard
    // Enter on any button anywhere "confirmed" a dialog nobody could see (Settings' unsaved-changes
    // one force-closed Settings).
    if (isOpen && event.key === 'Enter') {
      // Don't intercept Enter when user is typing in an input/textarea
      const target = event.target as HTMLElement;
      if (target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA' || target?.tagName === 'SELECT') {
        return;
      }
      // A focused button handles its own Enter; confirming on top of it would also confirm
      // when the user pressed Enter on Cancel.
      if (target?.tagName === 'BUTTON') return;
      handleConfirm();
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<BaseModal
  {isOpen}
  title={resolvedTitle}
  onClose={handleClose}
  size="sm"
  zIndex={1300}
  role={destructive ? 'alertdialog' : 'dialog'}
  describedBy="modal-message"
>
  <p id="modal-message" class="modal-message">{resolvedMessage}</p>

  <svelte:fragment slot="footer">
    {#if resolvedCancelText}
      <button
        class="btn btn-lg btn-secondary"
        data-testid="confirm-dialog-cancel"
        data-autofocus={destructive ? '' : undefined}
        on:click={handleCancel}
        type="button"
      >
        {resolvedCancelText}
      </button>
    {/if}
    <button
      class="btn btn-lg btn-{variant}"
      data-testid="confirm-dialog-confirm"
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
</style>
