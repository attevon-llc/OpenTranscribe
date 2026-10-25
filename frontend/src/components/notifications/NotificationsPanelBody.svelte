<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { buildJobModel, type ProcessingJobModel } from '$lib/notifications/processingJob';
  import { countUnreadItems, groupPanelItems } from '$lib/notifications/jobGrouping';
  import { reconcileJobs } from '$lib/notifications/reconcile';
  import { websocketStore } from '$stores/websocket';
  import { showNotificationsPanel } from '$stores/notificationsPanel';
  import { isLLMAvailable } from '$stores/llmStatus';
  import { t } from '$stores/locale';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ClearedNotice from './ClearedNotice.svelte';
  import NotificationRetentionSetting from './NotificationRetentionSetting.svelte';
  import NotificationRow from './NotificationRow.svelte';
  import ProcessingJobCard from './ProcessingJobCard.svelte';
  import JobAnnouncer from './JobAnnouncer.svelte';

  /**
   * Everything inside the open panel. Loaded on first open by `NotificationsPanel.svelte` so
   * the card model and its components stay out of the root layout bundle; mounted only while
   * the panel is open, so its clock runs only then.
   */

  /** Re-evaluates relative times, the plan grace and staleness while the panel is open. */
  const CLOCK_MS = 30_000;

  let now = Date.now();
  let clock: ReturnType<typeof setInterval> | undefined;

  function noteUpdate(_list: unknown) {
    now = Date.now();
  }

  $: notifications = $websocketStore.notifications;
  $: noteUpdate(notifications);
  $: items = groupPanelItems(notifications);
  $: unread = countUnreadItems(notifications);
  $: jobModels = new Map<string, ProcessingJobModel>(
    items.flatMap((item) =>
      item.kind === 'job'
        ? [[item.key, buildJobModel(item, { llmAvailable: $isLLMAvailable, now })] as const]
        : []
    )
  );
  $: jobs = [...jobModels.values()];
  // A card that cannot tell what post-processing is doing (a missed frame) asks the server.
  $: reconcileJobs(jobs, now, websocketStore.applyPostProcessingSnapshot);

  function closePanel() {
    showNotificationsPanel.set(false);
  }

  function openFile(event: CustomEvent<{ ids: string[]; link: string }>) {
    const { ids, link } = event.detail;
    for (const id of ids) {
      if (notifications.find((n) => n.id === id && !n.read)) websocketStore.markAsRead(id);
    }
    showNotificationsPanel.set(false);
    goto(link);
  }

  function dismiss(event: CustomEvent<{ ids: string[] }>) {
    for (const id of event.detail.ids) websocketStore.removeNotification(id);
  }

  onMount(() => {
    clock = setInterval(() => (now = Date.now()), CLOCK_MS);
  });

  onDestroy(() => {
    if (clock) clearInterval(clock);
  });
</script>

<div class="notifications-header">
  <div class="header-left">
    <h3 class="header-title">{$t('notifications.title')}</h3>
    {#if unread > 0}
      <span class="unread-badge">{unread}</span>
    {/if}
  </div>
  <div class="header-actions">
    {#if unread > 0}
      <button
        class="icon-btn mark-read-btn"
        on:click={() => websocketStore.markAllAsRead()}
        title={$t('notifications.markAllRead')}
        aria-label={$t('notifications.markAllRead')}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="20 6 9 17 4 12"></polyline>
        </svg>
      </button>
    {/if}
    {#if items.length > 0}
      <button
        class="icon-btn clear-btn"
        on:click={() => websocketStore.clearAll()}
        title={$t('notifications.clearAll')}
        aria-label={$t('notifications.clearAll')}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="3 6 5 6 21 6"></polyline>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
        </svg>
      </button>
    {/if}
    <button
      class="icon-btn close-btn"
      on:click={closePanel}
      title={$t('notifications.closePanel')}
      aria-label={$t('notifications.closePanel')}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <line x1="18" y1="6" x2="6" y2="18"></line>
        <line x1="6" y1="6" x2="18" y2="18"></line>
      </svg>
    </button>
  </div>
</div>

<ClearedNotice />
<JobAnnouncer {jobs} />

<div class="notifications-content">
  {#if items.length === 0}
    <EmptyState title={$t('notifications.empty')} description={$t('notifications.allCaughtUp')}>
      <svelte:fragment slot="icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
          <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
        </svg>
      </svelte:fragment>
    </EmptyState>
  {:else}
    <ul class="notifications-list">
      {#each items as item (item.key)}
        {#if item.kind === 'job'}
          {@const job = jobModels.get(item.key)}
          {#if job}
            <ProcessingJobCard {job} {now} on:open={openFile} on:dismiss={dismiss} />
          {/if}
        {:else}
          <NotificationRow notification={item.notification} {now} on:open={openFile} on:dismiss={dismiss} />
        {/if}
      {/each}
    </ul>
  {/if}
</div>

<NotificationRetentionSetting />

<style>
  .notifications-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border-color);
    background: var(--surface-color);
    border-radius: 12px 12px 0 0;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .header-title {
    margin: 0;
    font-size: 16px;
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .unread-badge {
    background: var(--primary-color);
    color: white;
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    padding: 2px 6px;
    border-radius: 10px;
    min-width: 16px;
    height: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    padding: 0.5rem;
    border-radius: 4px;
    cursor: pointer;
    transition:
      background-color 0.2s ease,
      color 0.2s ease;
    border: 1px solid;
    box-sizing: border-box;
  }

  .mark-read-btn {
    background-color: transparent;
    border-color: var(--success-color);
    color: var(--success-color);
  }

  .mark-read-btn:hover {
    background-color: var(--success-color);
    color: white;
  }

  .clear-btn {
    background-color: transparent;
    border-color: var(--error-color);
    color: var(--error-color);
  }

  .clear-btn:hover {
    background-color: var(--error-color);
    color: white;
  }

  .close-btn {
    background-color: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .close-btn:hover {
    background-color: var(--primary-color);
    border-color: var(--primary-color);
    color: white;
  }

  .notifications-content {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
  }

  .notifications-list {
    display: flex;
    flex-direction: column;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .notifications-content::-webkit-scrollbar {
    width: 6px;
  }

  .notifications-content::-webkit-scrollbar-track {
    background: transparent;
  }

  .notifications-content::-webkit-scrollbar-thumb {
    background: var(--border-color);
    border-radius: 3px;
  }

  .notifications-content::-webkit-scrollbar-thumb:hover {
    background: var(--text-secondary);
  }

  @media (max-width: 480px) {
    .notifications-header {
      padding: 14px 16px;
    }

    .header-title {
      font-size: 15px;
    }

    .icon-btn {
      width: 30px;
      height: 30px;
    }

    .header-actions {
      gap: 6px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .icon-btn {
      transition: none;
    }
  }
</style>
