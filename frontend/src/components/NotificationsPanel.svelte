<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { showNotificationsPanel } from '$stores/notificationsPanel';
  import { t } from '$stores/locale';

  /**
   * The bell panel's always-mounted shell (mounted once, by routes/+layout.svelte): open state,
   * scroll lock, click-outside and the backdrop. The contents are loaded on first open, which
   * keeps the processing-card model out of the root layout's static bundle (bundle budget).
   */
  type Body = typeof import('./notifications/NotificationsPanelBody.svelte').default;

  let showPanel = false;
  const unsubscribePanel = showNotificationsPanel.subscribe((value) => {
    showPanel = value;
  });

  let panelWasOpen = false;
  $: if (typeof document !== 'undefined' && showPanel !== panelWasOpen) {
    if (showPanel) lockScroll();
    else unlockScroll();
    panelWasOpen = showPanel;
  }

  let body: Body | null = null;
  let loading: Promise<void> | null = null;
  $: if (showPanel && !body && !loading) {
    loading = import('./notifications/NotificationsPanelBody.svelte')
      .then((m) => {
        body = m.default;
      })
      .finally(() => {
        // A failed chunk load (offline, deploy in progress) is retried on the next open.
        loading = null;
      });
  }

  function closePanel() {
    showNotificationsPanel.set(false);
  }

  function handleClickOutside(event: MouseEvent) {
    const panel = document.querySelector('.notifications-panel');
    const button = document.querySelector('.notifications-btn');
    const target = event.target;
    if (panel && target instanceof Node && !panel.contains(target) && !button?.contains(target)) {
      showNotificationsPanel.set(false);
    }
  }

  onMount(() => {
    document.addEventListener('click', handleClickOutside);
  });

  onDestroy(() => {
    document.removeEventListener('click', handleClickOutside);
    unsubscribePanel();
    if (panelWasOpen) unlockScroll();
  });
</script>

{#if showPanel}
  <div
    class="notifications-backdrop"
    on:click={closePanel}
    on:keydown={closePanel}
    role="button"
    tabindex="0"
    aria-label={$t('notifications.closePanel')}
  ></div>

  <div class="notifications-panel">
    {#if body}
      <svelte:component this={body} />
    {/if}
  </div>
{/if}

<style>
  .notifications-backdrop {
    position: fixed;
    inset: 0;
    background: transparent;
    z-index: 999;
  }

  .notifications-panel {
    position: fixed;
    top: 70px;
    inset-inline-end: 16px;
    width: 380px;
    max-width: calc(100vw - 32px);
    max-height: calc(100vh - 90px);
    max-height: calc(100dvh - 90px);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    box-shadow:
      0 20px 25px -5px rgba(0, 0, 0, 0.1),
      0 10px 10px -5px rgba(0, 0, 0, 0.04),
      0 0 0 1px rgba(0, 0, 0, 0.05);
    z-index: 1000;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  :global([data-theme='dark']) .notifications-panel {
    box-shadow:
      0 20px 25px -5px rgba(0, 0, 0, 0.4),
      0 10px 10px -5px rgba(0, 0, 0, 0.2),
      0 0 0 1px rgba(255, 255, 255, 0.05);
  }

  @media (max-width: 480px) {
    .notifications-panel {
      inset-inline: 8px;
      width: auto;
      max-width: none;
    }
  }

  @media (prefers-contrast: high) {
    .notifications-panel {
      border-width: 2px;
    }
  }
</style>
