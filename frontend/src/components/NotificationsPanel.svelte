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
  function syncScrollLock(open: boolean) {
    if (typeof document === 'undefined' || open === panelWasOpen) return;
    if (open) lockScroll();
    else unlockScroll();
    panelWasOpen = open;
  }

  $: syncScrollLock(showPanel);

  let body: Body | null = null;
  let loading: Promise<void> | null = null;
  function loadBody(open: boolean) {
    if (!open || body || loading) return;
    loading = import('./notifications/NotificationsPanelBody.svelte')
      .then((m) => {
        body = m.default;
      })
      .finally(() => {
        // A failed chunk load (offline, deploy in progress) is retried on the next open.
        loading = null;
      });
  }

  $: loadBody(showPanel);

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
    border-radius: var(--radius-2xl);
    box-shadow: var(--shadow-xl);
    z-index: var(--z-dropdown);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  @media (max-width: 480px) {
    .notifications-panel {
      inset-inline: 8px;
      width: auto;
      max-width: none;
    }
  }

  @media (prefers-contrast: more) {
    .notifications-panel {
      border-width: 2px;
    }
  }
</style>
