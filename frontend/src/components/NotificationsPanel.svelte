<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { derived } from 'svelte/store';
  import { goto } from '$app/navigation';
  import { token } from '../stores/auth';
  import { websocketStore } from '../stores/websocket';
  import { showNotificationsPanel } from '../stores/notificationsPanel';
  import { t } from '$stores/locale';
  import EmptyState from './ui/EmptyState.svelte';


  // Subscribe to the showNotificationsPanel store
  let showPanel = false;
  const unsubscribePanel = showNotificationsPanel.subscribe(value => {
    showPanel = value;
  });

  let _panelWasOpen = false;
  $: if (typeof document !== 'undefined' && showPanel !== _panelWasOpen) {
    showPanel ? lockScroll() : unlockScroll();
    _panelWasOpen = showPanel;
  }

  /** @type {Date} */
  let lastRead = new Date();

  // Derived store for unread count (excluding silent notifications)
  const wsUnreadCount = derived(websocketStore, ($store) => {
    return $store.notifications ? $store.notifications.filter(n => !n.read && !n.silent).length : 0;
  });

  // Toggle notification panel
  function togglePanel() {
    showNotificationsPanel.update(value => !value);
  }

  // Close the notifications panel
  function closePanel() {
    showNotificationsPanel.set(false);
    // Don't automatically mark as read - let user do it manually
  }

  /**
   * Close panel when clicking outside
   * @param {MouseEvent} event - The click event
   */
  function handleClickOutside(event: MouseEvent) {
    const panel = document.querySelector('.notifications-panel');
    const button = document.querySelector('.notifications-button');
    const target = event.target;

    if (panel && button && target instanceof Node && !panel.contains(target) && !button.contains(target)) {
      showNotificationsPanel.set(false);
    }
  }

  /**
   * Remove a notification
   * @param {string} id - The notification ID
   */
  function removeNotification(id: string) {
    // Use the new removeNotification method from websocket store
    if (websocketStore.removeNotification) {
      websocketStore.removeNotification(id);
    } else {
      // Fallback to marking as read
      websocketStore.markAsRead(id);
    }
  }

  // Clear all notifications
  function clearAllNotifications() {
    websocketStore.clearAll();
  }

  // Mark all notifications as read
  function markAllWebSocketNotificationsAsRead() {
    if (websocketStore.markAllAsRead) {
      websocketStore.markAllAsRead();
    }
  }

  /**
   * Get appropriate icon for notification type
   * @param {string} type - The notification type
   * @returns {string} - Icon name
   */
  function getNotificationIcon(type: string) {
    switch (type) {
      case 'transcription_status':
        return 'file-text';
      case 'summarization_status':
        return 'file-text';
      case 'youtube_processing_status':
        return 'video';
      case 'audio_extraction_status':
        return 'music';
      case 'analytics_status':
        return 'bar-chart';
      case 'reindex_progress':
      case 'reindex_complete':
      case 'reindex_stopped':
        return 'search';
      case 'migration_progress':
      case 'migration_complete':
      case 'migration_finalized':
        return 'database';
      case 'clustering_progress':
      case 'clustering_complete':
        return 'users';
      case 'attribute_migration_progress':
      case 'attribute_migration_complete':
        return 'users';
      default:
        return 'bell';
    }
  }

  /**
   * Format timestamp to relative time (e.g. "5 minutes ago")
   * @param {Date} date - The date to format
   * @returns {string} - Formatted relative time
   */
  function formatTimestamp(date: Date) {
    const now = new Date();
    const diff = Math.floor((now.getTime() - date.getTime()) / 1000);

    if (diff < 60) {
      return $t('notifications.justNow');
    } else if (diff < 3600) {
      const minutes = Math.floor(diff / 60);
      return $t('notifications.minutesAgo', { count: minutes });
    } else if (diff < 86400) {
      const hours = Math.floor(diff / 3600);
      return $t('notifications.hoursAgo', { count: hours });
    } else {
      const days = Math.floor(diff / 86400);
      return $t('notifications.daysAgo', { count: days });
    }
  }

  /**
   * Get file link based on notification data
   * @param {Object} notification - The notification object
   * @param {Object} [notification.data] - Optional notification data
   * @param {string} [notification.data.file_id] - Optional file ID
   * @returns {string|null} - File link or null if no file_id
   */
  function getFileLink(notification: { data?: { file_id?: string } }) {
    if (notification.data && notification.data.file_id) {
      return `/files/${notification.data.file_id}`;
    }
    return null;
  }

  /**
   * Get display text for file link
   * @param {Object} notification - The notification object
   * @returns {string} - Display text for the file link
   */
  function getFileLinkText(notification: any): string {
    const fileId = notification.data?.file_id;
    if (!fileId) return $t('common.viewFile');

    // If we have filename in the notification data, use it
    if (notification.data?.filename) {
      const filename = notification.data.filename;
      // Truncate long filenames to ensure single line display
      if (filename.length > 40) {
        const extension = filename.split('.').pop() || '';
        const nameWithoutExt = filename.substring(0, filename.lastIndexOf('.'));
        // Truncate from the end, keep beginning + ... + extension
        const maxNameLength = 35; // Leave room for extension and arrow
        if (nameWithoutExt.length > maxNameLength) {
          const truncated = nameWithoutExt.substring(0, maxNameLength) + '...';
          return `${truncated}.${extension}`;
        }
      }
      return `${filename}`;
    }

    // Fallback: show file ID in a user-friendly way
    return $t('common.viewFile');
  }

  /**
   * Get status color for notification type
   * @param {Object} notification - The notification object
   * @returns {string} - CSS class for status
   */
  function getNotificationStatus(notification: { data?: { status?: string } }) {
    const notificationData = notification.data || {};
    if (notificationData.status) {
      const status = notificationData.status;
      switch (status) {
        case 'completed':
          return 'success';
        case 'error':
        case 'failed':
          return 'error';
        case 'processing':
        case 'in_progress':
          return 'info';
        case 'not_configured':
        case 'warning':
          return 'warning';
        default:
          return 'default';
      }
    }
    return 'default';
  }

  onMount(() => {
    // Add event listener for clicks outside the panel
    document.addEventListener('click', handleClickOutside);
  });

  onDestroy(() => {
    document.removeEventListener('click', handleClickOutside);
    unsubscribePanel();
    if (_panelWasOpen) unlockScroll();
  });
</script>

{#if showPanel}
  <!-- Backdrop for mobile/tablet -->
  <div class="notifications-backdrop" on:click={closePanel} on:keydown={closePanel} role="button" tabindex="0" aria-label={$t('notifications.closePanel')}></div>

  <div class="notifications-panel">
    <!-- Header -->
    <div class="notifications-header">
      <div class="header-left">
        <h3 class="header-title">{$t('notifications.title')}</h3>
        {#if $wsUnreadCount > 0}
          <span class="unread-badge">{$wsUnreadCount}</span>
        {/if}
      </div>
      <div class="header-actions">
        {#if $wsUnreadCount > 0}
          <button
            class="icon-btn mark-read-btn"
            on:click={markAllWebSocketNotificationsAsRead}
            title={$t('notifications.markAllRead')}
            aria-label={$t('notifications.markAllRead')}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="20 6 9 17 4 12"></polyline>
            </svg>
          </button>
        {/if}
        {#if $websocketStore.notifications.filter(n => !n.silent).length > 0}
          <button
            class="icon-btn clear-btn"
            on:click={clearAllNotifications}
            title={$t('notifications.clearAll')}
            aria-label={$t('notifications.clearAll')}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
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
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
    </div>

    <!-- Notifications List -->
    <div class="notifications-content">
      {#if $websocketStore.notifications.filter(n => !n.silent).length === 0}
        <EmptyState title={$t('notifications.empty')} description={$t('notifications.allCaughtUp')}>
          <svelte:fragment slot="icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
              <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
            </svg>
          </svelte:fragment>
        </EmptyState>
      {:else}
        <div class="notifications-list">
          {#each $websocketStore.notifications.filter(n => !n.silent) as notification (notification.id)}
            <div class="notification-item {notification.read ? 'read' : 'unread'} status-{getNotificationStatus(notification)} {notification.status === 'processing' ? 'processing' : ''}">
              <!-- Status indicator -->
              <div class="notification-indicator"></div>

              <!-- Icon -->
              <div class="notification-icon">
                {#if getNotificationIcon(notification.type) === 'file-text'}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                    <polyline points="14 2 14 8 20 8"></polyline>
                    <line x1="16" y1="13" x2="8" y2="13"></line>
                    <line x1="16" y1="17" x2="8" y2="17"></line>
                  </svg>
                {:else if getNotificationIcon(notification.type) === 'bar-chart'}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="18" y1="20" x2="18" y2="10"></line>
                    <line x1="12" y1="20" x2="12" y2="4"></line>
                    <line x1="6" y1="20" x2="6" y2="14"></line>
                  </svg>
                {:else if getNotificationIcon(notification.type) === 'search'}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="11" cy="11" r="8"></circle>
                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                  </svg>
                {:else if getNotificationIcon(notification.type) === 'database'}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
                    <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path>
                    <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path>
                  </svg>
                {:else if getNotificationIcon(notification.type) === 'users'}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
                    <circle cx="9" cy="7" r="4"></circle>
                    <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
                    <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
                  </svg>
                {:else}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
                    <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
                  </svg>
                {/if}
              </div>

              <!-- Content -->
              <div class="notification-content">
                <div class="notification-main">
                  <h4 class="notification-title">{notification.title}</h4>
                  <p class="notification-message">{notification.message}</p>

                  <!-- Progressive notification progress bar -->
                  {#if notification.progress && notification.status === 'processing'}
                    <div class="progress-container">
                      <div class="progress-bar-bg">
                        <div class="progress-bar-fill" style="width: {notification.progress.percentage}%"></div>
                      </div>
                      <span class="progress-text">
                        {notification.progress.percentage}%
                        {#if notification.progress.etaDisplay}
                          ({notification.progress.etaDisplay} {$t('upload.remaining')})
                        {/if}
                      </span>
                    </div>
                  {/if}

                  <!-- Enrichment task chips -->
                  {#if notification.completedEnrichments && notification.completedEnrichments.length > 0}
                    <div class="enrichment-chips">
                      {#each notification.completedEnrichments as task}
                        <span class="enrichment-chip">
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="20 6 9 17 4 12"></polyline>
                          </svg>
                          {$t(`notifications.enrichment.${task}`)}
                        </span>
                      {/each}
                    </div>
                  {/if}

                  <!-- Action link if available -->
                  {#if getFileLink(notification) !== null}
                    <a
                      href={getFileLink(notification) || ''}
                      class="notification-action"
                      on:click|preventDefault={() => {
                        const link = getFileLink(notification);
                        showPanel = false;
                        // Mark this specific notification as read when navigating to file
                        if (!notification.read) {
                          websocketStore.markAsRead(notification.id);
                        }
                        if (link) goto(link);
                      }}
                    >
{getFileLinkText(notification)}
                    </a>
                  {/if}
                </div>

                <div class="notification-meta">
                  <span class="notification-time">{formatTimestamp(notification.timestamp)}</span>
                </div>
              </div>

              <!-- Dismiss button -->
              <button
                class="notification-dismiss {!notification.dismissible ? 'non-dismissible' : ''}"
                on:click={() => removeNotification(notification.id)}
                title={notification.dismissible ? $t('notifications.dismiss') : $t('notifications.processingCannotDismiss')}
                disabled={!notification.dismissible}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18"></line>
                  <line x1="6" y1="6" x2="18" y2="18"></line>
                </svg>
              </button>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .notifications-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: transparent;
    z-index: 999;
  }

  .notifications-panel {
    position: fixed;
    top: 70px;
    right: 16px;
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

  /* Header */
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
    font-weight: 600;
    color: var(--text-color);
  }

  .unread-badge {
    background: #3b82f6;
    color: white;
    font-size: 11px;
    font-weight: 600;
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
    transition: all 0.2s ease;
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
    background-color: #3b82f6;
    border-color: #3b82f6;
    color: white;
  }

  /* Content */
  .notifications-content {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
  }

  .notifications-list {
    display: flex;
    flex-direction: column;
  }

  /* Notification Items */
  .notification-item {
    position: relative;
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border-color);
    transition: all 0.2s ease;
    background: var(--background-color);
  }

  .notification-item:last-child {
    border-bottom: none;
  }

  .notification-item:hover {
    background: var(--hover-color, rgba(0, 0, 0, 0.025));
  }

  .notification-item.unread {
    background: rgba(59, 130, 246, 0.04);
  }

  :global([data-theme='dark']) .notification-item.unread {
    background: rgba(59, 130, 246, 0.08);
  }

  /* Status indicators */
  .notification-indicator {
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 3px;
    background: transparent;
  }

  .notification-item.status-success .notification-indicator {
    background: var(--success-color);
  }

  .notification-item.status-error .notification-indicator {
    background: var(--error-color);
  }

  .notification-item.status-info .notification-indicator {
    background: #3b82f6;
  }

  .notification-item.status-warning .notification-indicator {
    background: var(--warning-color, #f59e0b);
  }

  .notification-item.unread .notification-indicator {
    background: #3b82f6;
  }

  .notification-item.processing .notification-indicator {
    background: #3b82f6;
    animation: pulse 1.5s infinite;
  }

  @keyframes pulse {
    0%, 100% {
      opacity: 1;
    }
    50% {
      opacity: 0.5;
    }
  }

  /* Icon */
  .notification-icon {
    flex-shrink: 0;
    width: 32px;
    height: 32px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    margin-top: 2px;
  }

  .notification-item.status-success .notification-icon {
    background: rgba(16, 185, 129, 0.1);
    border-color: rgba(16, 185, 129, 0.2);
    color: var(--success-color);
  }

  .notification-item.status-error .notification-icon {
    background: rgba(239, 68, 68, 0.1);
    border-color: rgba(239, 68, 68, 0.2);
    color: var(--error-color);
  }

  .notification-item.status-info .notification-icon {
    background: rgba(59, 130, 246, 0.1);
    border-color: rgba(59, 130, 246, 0.2);
    color: var(--primary-color);
  }

  .notification-item.status-warning .notification-icon {
    background: rgba(245, 158, 11, 0.1);
    border-color: rgba(245, 158, 11, 0.2);
    color: var(--warning-color, #f59e0b);
  }

  /* Content */
  .notification-content {
    flex: 1;
    min-width: 0;
  }

  .notification-main {
    margin-bottom: 8px;
  }

  .notification-title {
    margin: 0 0 4px 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-color);
    line-height: 1.4;
  }

  .notification-message {
    margin: 0 0 8px 0;
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.4;
    word-wrap: break-word;
  }

  .enrichment-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin: 0 0 8px 0;
  }

  .enrichment-chip {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 500;
    color: var(--success-color, #10b981);
    background: rgba(16, 185, 129, 0.1);
    border-radius: 10px;
    line-height: 1.4;
    white-space: nowrap;
  }

  .enrichment-chip svg {
    flex-shrink: 0;
  }

  .notification-action {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--primary-color);
    text-decoration: none;
    transition: color 0.2s ease;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
    line-height: 1.2;
  }

  .notification-action:hover {
    color: var(--primary-color-dark, #2563eb);
    text-decoration: none;
  }

  .notification-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .notification-time {
    font-size: 11px;
    font-weight: 500;
    color: var(--text-secondary);
    opacity: 0.8;
  }

  /* Dismiss Button */
  .notification-dismiss {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    background: none;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s ease;
    opacity: 0.6;
  }

  .notification-dismiss:hover {
    background: var(--error-background, rgba(239, 68, 68, 0.1));
    color: var(--error-color);
    opacity: 1;
  }

  .notification-dismiss.non-dismissible {
    opacity: 0.3;
    cursor: not-allowed;
  }

  .notification-dismiss.non-dismissible:hover {
    background: none;
    color: var(--text-secondary);
    opacity: 0.3;
  }

  /* Progress Bar Styles */
  .progress-container {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
  }

  .progress-bar-bg {
    flex: 1;
    height: 4px;
    background: var(--border-color);
    border-radius: 2px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: #3b82f6;
    border-radius: 2px;
    transition: width 0.3s ease;
    animation: progress-shimmer 1.5s infinite;
  }

  @keyframes progress-shimmer {
    0% {
      background-position: -200px 0;
    }
    100% {
      background-position: calc(200px + 100%) 0;
    }
  }

  .progress-bar-fill {
    background: linear-gradient(
      90deg,
      var(--primary-color) 0%,
      rgba(var(--primary-color-rgb), 0.8) 50%,
      var(--primary-color) 100%
    );
    background-size: 200px 100%;
    animation: progress-shimmer 1.5s infinite linear;
  }

  .progress-text {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
    min-width: 32px;
    text-align: right;
  }

  /* Scrollbar Styling */
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

  /* Responsive Design */
  @media (max-width: 480px) {
    .notifications-panel {
      right: 8px;
      left: 8px;
      width: auto;
      max-width: none;
    }

    .notifications-header {
      padding: 14px 16px;
    }

    .notification-item {
      padding: 14px 16px;
      gap: 10px;
    }

    .notification-icon {
      width: 28px;
      height: 28px;
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

  /* Reduced motion support */
  @media (prefers-reduced-motion: reduce) {
    .notification-item,
    .icon-btn,
    .notification-dismiss,
    .notification-action {
      transition: none;
    }

    .icon-btn:hover {
      transform: none;
    }

    .icon-btn:active {
      transform: none;
    }
  }

  /* High contrast mode support */
  @media (prefers-contrast: high) {
    .notifications-panel {
      border-width: 2px;
    }

    .notification-item {
      border-bottom-width: 2px;
    }

    .notification-indicator {
      width: 4px;
    }
  }
</style>
