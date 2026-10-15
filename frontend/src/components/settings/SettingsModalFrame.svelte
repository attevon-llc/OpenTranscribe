<!--
  SettingsModalFrame.svelte — the dialog chrome of Settings: backdrop, title bar, close button,
  sidebar column and scrolling content pane. It holds no settings logic.

  Two hosts render it: `SettingsModalShell` (skeleton contents, painted the instant the modal is
  asked for, before the heavy chunk has arrived) and `SettingsModal` (the real thing). Sharing
  one frame is what makes the hand-off between them invisible: same geometry, same classes.

  E2E-guarded class names: `.settings-modal`, `.settings-sidebar`, `.settings-content`.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  /** The `.settings-modal` dialog element (for hosts that query inside it). */
  export let element: HTMLElement | undefined = undefined;
  /** Skip the enter animation, so a real modal replacing the shell does not replay it. */
  export let skipEnter = false;
  /** The contents are placeholders: the dialog is announced as busy. */
  export let busy = false;

  const dispatch = createEventDispatcher<{ close: void }>();

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) dispatch('close');
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="settings-modal-backdrop"
  class:skip-enter={skipEnter}
  on:click={handleBackdropClick}
  on:wheel|preventDefault|self
  on:touchmove|preventDefault|self
  role="presentation"
>
  <div
    class="settings-modal"
    bind:this={element}
    role="dialog"
    aria-modal="true"
    aria-labelledby="settings-modal-title"
    aria-busy={busy}
    data-loading={busy ? 'true' : undefined}
  >
    <div class="settings-header-bar">
      <h2 id="settings-modal-title" class="settings-header-title">{$t('settings.title')}</h2>
      <button
        class="modal-close-button"
        on:click={() => dispatch('close')}
        aria-label={$t('settings.modal.closeSettings')}
        title={$t('settings.modal.closeSettingsTitle')}
        style="position:static; margin:0; padding:0.5rem;"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>

    <div class="settings-modal-container">
      <aside class="settings-sidebar">
        <slot name="sidebar" />
      </aside>

      <main class="settings-content">
        <slot />
      </main>
    </div>
  </div>
</div>

<style>
  .settings-modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: var(--modal-backdrop);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: var(--z-modal);
    animation: fadeIn 0.2s ease-out;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .settings-modal {
    position: relative;
    width: 90vw;
    max-width: 1200px;
    height: 85vh;
    max-height: 900px;
    background-color: var(--surface-color);
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    overflow: hidden;
    animation: slideUp 0.3s ease-out;
    display: flex;
    flex-direction: column;
  }

  .settings-modal-backdrop.skip-enter,
  .settings-modal-backdrop.skip-enter .settings-modal {
    animation: none;
  }

  .settings-header-bar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
    min-height: 44px;
  }

  .settings-header-title {
    font-weight: 600;
    font-size: 1.1rem;
    color: var(--text-color);
    margin: 0;
    margin-right: auto;
  }

  .modal-close-button {
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
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
    z-index: 10;
  }

  .modal-close-button:hover {
    color: var(--text-color);
    background: var(--button-hover, var(--background-color));
  }

  .settings-modal-container {
    display: flex;
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }

  /* A column, not a scroller: the search is a fixed header and only the nav list scrolls, so a
     scrollbar can never sit on top of the input. */
  .settings-sidebar {
    display: flex;
    flex-direction: column;
    width: 240px;
    background-color: var(--background-color);
    border-right: 1px solid var(--border-color);
    overflow: hidden;
    flex-shrink: 0;
  }

  /* The tab strips of tabbed sections pin to this pane's scrollport (see ui/Tabs.svelte); the
     header bar above is outside it, so a pinned strip always sits just below the title. */
  .settings-content {
    --tabs-sticky-inset: 1.5rem;
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
    overscroll-behavior: contain;
  }

  /* Raise above navbar (z-index 1200) on tablet so close button is reachable */
  @media (max-width: 768px) {
    .settings-modal {
      width: 100vw;
      height: 100vh;
      height: 100dvh;
      max-width: none;
      max-height: none;
      border-radius: 0;
      padding-top: env(safe-area-inset-top, 0px);
      padding-bottom: env(safe-area-inset-bottom, 0px);
    }

    .settings-modal-container {
      flex-direction: column;
      overflow: visible;
    }

    /* Hide full sidebar on mobile — use select dropdown */
    .settings-sidebar {
      display: none;
    }

    .settings-header-bar {
      padding: 0.75rem 1rem;
    }

    .settings-content {
      --tabs-sticky-inset: 1rem;
      padding: 1rem;
      flex: 1 1 0;
      min-height: 0;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
      overflow-x: hidden;
    }

    /* Global mobile overrides for all settings panels */
    .settings-content :global(.form-group) {
      min-width: 0;
    }

    .settings-content :global(input),
    .settings-content :global(select),
    .settings-content :global(textarea) {
      min-height: 44px;
      font-size: 1rem;
      max-width: 100%;
      box-sizing: border-box;
    }

    .settings-content :global(button) {
      min-height: 44px;
    }
  }
</style>
