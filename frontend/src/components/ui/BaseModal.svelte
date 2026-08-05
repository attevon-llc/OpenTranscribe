<!--
  BaseModal.svelte — Shared modal wrapper component.

  Extracts common modal chrome (backdrop, container, header, close button,
  body, footer) that was duplicated across 6+ modal components.

  Usage:
    <BaseModal {isOpen} title="My Modal" onClose={handleClose}>
      <p>Modal body content</p>
      <svelte:fragment slot="footer">
        <button on:click={handleClose}>Cancel</button>
        <button on:click={handleSave}>Save</button>
      </svelte:fragment>
    </BaseModal>
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import { onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { focusTrap } from '$lib/actions/focusTrap';

  export let isOpen = false;
  export let title = '';
  export let maxWidth = '600px';
  export let zIndex = 1300;
  export let onClose: () => void = () => {};

  // Stable id for wiring the dialog's accessible name to its <h2> title (when no
  // custom header slot is supplied). Generated once per instance.
  const titleId = `modal-title-${Math.random().toString(36).slice(2, 10)}`;

  let _wasOpen = false;
  $: if (typeof document !== 'undefined' && isOpen !== _wasOpen) {
    isOpen ? lockScroll() : unlockScroll();
    _wasOpen = isOpen;
  }

  onDestroy(() => {
    if (_wasOpen) unlockScroll();
  });

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) onClose();
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onClose();
  }
</script>

<svelte:window on:keydown={handleKeydown} />

{#if isOpen}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-backdrop" style="z-index: {zIndex}" on:click={handleBackdropClick} on:wheel|preventDefault|self on:touchmove|preventDefault|self>
    <div
      class="modal-container"
      style="max-width: {maxWidth}"
      role="dialog"
      aria-modal="true"
      aria-labelledby={$$slots.header ? undefined : titleId}
      aria-label={$$slots.header ? title || undefined : undefined}
      use:focusTrap={{ enabled: isOpen }}
    >
      <div class="modal-header">
        {#if $$slots.header}
          <div class="modal-header-content">
            <slot name="header" />
          </div>
        {:else}
          <h2 id={titleId}>{title}</h2>
        {/if}
        <button class="modal-close-button" on:click={onClose} aria-label={$t('modal.closeDialog')}>
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <slot />
      </div>
      {#if $$slots.footer}
        <div class="modal-footer">
          <slot name="footer" />
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background-color: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .modal-container {
    background: var(--bg-primary, #ffffff);
    border: 1px solid var(--border-color, #e0e0e0);
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    width: 90%;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    animation: slideIn 0.2s ease-out;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid var(--border-color, #e0e0e0);
    flex-shrink: 0;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    color: var(--text-primary, #1a1a1a);
  }

  .modal-header-content {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex: 1;
    min-width: 0;
    gap: 0.75rem;
  }

  .modal-close-button {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary, #666);
    padding: 0.5rem;
    margin-left: 1rem;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: color 0.2s ease, background-color 0.2s ease;
    flex-shrink: 0;
  }

  .modal-close-button:hover {
    color: var(--text-color, var(--text-primary, #1a1a1a));
    background-color: var(--button-hover, var(--bg-hover, #f0f0f0));
  }

  .modal-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
    overscroll-behavior: contain;
  }

  .modal-footer {
    display: flex;
    gap: 0.75rem;
    padding: 1rem 1.5rem;
    justify-content: flex-end;
    border-top: 1px solid var(--border-color, #e0e0e0);
    flex-shrink: 0;
  }

  @keyframes slideIn {
    from {
      opacity: 0;
      transform: translateY(-10px) scale(0.98);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }

  /* Raise modals above navbar (z-index 1200) on mobile/tablet so
     close button is not hidden behind the navbar */
  @media (max-width: 1200px) {
    .modal-backdrop {
      z-index: 1300 !important;
    }
  }

  @media (max-width: 768px) {
    .modal-container {
      width: 100%;
      max-width: 100% !important;
      max-height: 100%;
      max-height: 100dvh;
      border-radius: 0;
      margin: 0;
    }

    .modal-backdrop {
      align-items: stretch;
    }

    .modal-header {
      padding: 1rem;
      padding-top: calc(1rem + env(safe-area-inset-top, 0px));
    }

    .modal-header h2 {
      font-size: 1.1rem;
    }

    .modal-body {
      padding: 1rem;
    }

    .modal-footer {
      padding: 0.75rem 1rem;
      padding-bottom: calc(0.75rem + env(safe-area-inset-bottom, 0px));
      flex-wrap: wrap;
    }

    .modal-footer :global(button) {
      min-height: 44px;
    }
  }
</style>
