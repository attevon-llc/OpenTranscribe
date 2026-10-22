<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { Notification } from '$stores/websocket';
  import {
    fileLinkFor,
    formatRelativeTime,
    iconFor,
    notificationTone,
    shortenFilename,
  } from '$lib/notifications/display';
  import NotificationShell from './NotificationShell.svelte';
  import NotificationProgress from './NotificationProgress.svelte';

  /** A notification that is not part of a file's processing card (admin, sharing, ...). */
  export let notification: Notification;
  export let now: number;

  const dispatch = createEventDispatcher<{
    open: { ids: string[]; link: string };
    dismiss: { ids: string[] };
  }>();

  $: link = fileLinkFor(notification.data?.file_id);
  $: filename = notification.data?.filename;
  $: processing = notification.status === 'processing';
</script>

<NotificationShell
  icon={iconFor(notification.type)}
  tone={notificationTone(notification)}
  read={notification.read}
  {processing}
  dismissible={!!notification.dismissible}
  on:dismiss={() => dispatch('dismiss', { ids: [notification.id] })}
>
  <div class="notification-main">
    <h4 class="notification-title">{notification.title}</h4>
    <p class="notification-message">{notification.message}</p>

    {#if notification.progress && processing}
      <NotificationProgress
        percent={notification.progress.percentage}
        label={notification.title}
        eta={notification.progress.etaDisplay ?? ''}
      />
    {/if}

    {#if link}
      <a
        href={link}
        class="notification-action"
        on:click|preventDefault={() => dispatch('open', { ids: [notification.id], link })}
      >
        {typeof filename === 'string' && filename ? shortenFilename(filename) : $t('common.viewFile')}
      </a>
    {/if}
  </div>

  <div class="notification-meta">
    <span class="notification-time">{formatRelativeTime(notification.timestamp, now, $t)}</span>
  </div>
</NotificationShell>

<style>
  .notification-main {
    margin-bottom: 8px;
  }

  .notification-title {
    margin: 0 0 4px 0;
    font-size: var(--font-size-md, 14px);
    font-weight: var(--font-weight-semibold, 600);
    color: var(--text-color);
    line-height: 1.4;
  }

  .notification-message {
    margin: 0 0 8px 0;
    font-size: var(--font-size-sm, 13px);
    color: var(--text-secondary);
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .notification-action {
    display: block;
    margin-top: 8px;
    font-size: var(--font-size-xs, 12px);
    font-weight: var(--font-weight-semibold, 600);
    color: var(--primary-on-surface);
    text-decoration: none;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
    line-height: 1.2;
  }

  .notification-action:hover {
    text-decoration: underline;
  }

  .notification-time {
    font-size: var(--font-size-2xs, 11px);
    font-weight: var(--font-weight-medium, 500);
    color: var(--text-secondary);
  }
</style>
