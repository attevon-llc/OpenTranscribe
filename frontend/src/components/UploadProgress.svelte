<script lang="ts">
  import type { UploadItem } from '../lib/services/uploadService';
  import { uploadsStore } from '../stores/uploads';
  import { t } from '$stores/locale';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import type { MenuIconName } from '$lib/ui/menuIcons';

  export let upload: UploadItem;

  // Format file size
  function formatFileSize(bytes?: number): string {
    if (!bytes) return '';

    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${sizes[i]}`;
  }

  // Get status color
  function getStatusColor(status: string): string {
    switch (status) {
      case 'completed': return 'var(--success-color)';
      case 'failed': return 'var(--error-color)';
      case 'cancelled': return 'var(--text-secondary)';
      // The fill sits on --border-color; --primary-on-surface is the blue that
      // keeps >=3:1 against it in both themes (--primary-color does not in dark).
      case 'uploading':
      case 'processing':
      case 'preparing': return 'var(--primary-on-surface)';
      default: return 'var(--warning-color)';
    }
  }

  type Status = UploadItem['status'];

  // A closed map, not a switch with a `default`: the old switch drew an hourglass emoji
  // for any status it did not list, and that placeholder is what flashed in the tray.
  const STATUS_ICON: Record<Status, MenuIconName> = {
    queued: 'status-queued',
    preparing: 'status-working',
    uploading: 'upload',
    processing: 'status-working',
    completed: 'status-success',
    failed: 'status-error',
    cancelled: 'status-cancelled',
  };
  const STATUS_LABEL_KEY: Record<Status, string> = {
    queued: 'upload.statusLabel.queued',
    preparing: 'upload.statusLabel.preparing',
    uploading: 'upload.statusLabel.uploading',
    processing: 'upload.statusLabel.processing',
    completed: 'upload.statusLabel.completed',
    failed: 'upload.statusLabel.failed',
    cancelled: 'upload.statusLabel.cancelled',
  };
  const SPINNING: ReadonlySet<Status> = new Set<Status>(['preparing', 'processing']);

  // Queued rows keep the (empty) progress track: a retry passes failed -> queued ->
  // preparing, and hiding the track for the queued step made the row collapse and regrow.
  $: showProgress =
    upload.status === 'queued' ||
    upload.status === 'uploading' ||
    upload.status === 'processing' ||
    upload.status === 'preparing';

  // Handle actions
  function handleRetry() {
    uploadsStore.retry(upload.id);
  }

  function handleCancel() {
    uploadsStore.cancel(upload.id);
  }

  function handleRemove() {
    uploadsStore.remove(upload.id);
  }
</script>

<div class="upload-item">
  <div class="upload-header">
    <div class="upload-info">
      <span
        class="upload-icon"
        style="color: {getStatusColor(upload.status)}"
        role="img"
        aria-label={$t(STATUS_LABEL_KEY[upload.status])}
        title={$t(STATUS_LABEL_KEY[upload.status])}
        data-status={upload.status}
      >
        <MenuIcon
          name={STATUS_ICON[upload.status]}
          size="md"
          spin={SPINNING.has(upload.status)}
        />
      </span>
      <div class="upload-details">
        <div class="upload-name" title={upload.name}>
          {upload.name}
        </div>
        <div class="upload-meta">
          <span class="upload-type">{upload.type}</span>
          {#if upload.size}
            <span class="upload-size" dir="ltr">{formatFileSize(upload.size)}</span>
          {/if}
          {#if upload.estimatedTime}
            <!-- `estimatedTime` is a DURATION only. It used to double as the
                 phase-status field, so this row rendered "Calculating file
                 hash... remaining". Phase text now has its own field. -->
            <span class="upload-time">{upload.estimatedTime} {$t('upload.remaining')}</span>
          {:else if upload.statusText}
            <span class="upload-status-text">{upload.statusText}</span>
          {/if}
          {#if upload.dedupSkipped}
            <!-- The fingerprint failed, so this upload was never checked against
                 the library. Say so — a silently-skipped duplicate check is the
                 bug this replaced (issue #342). -->
            <span class="upload-dedup-skipped" title={$t('upload.dedupSkippedBadge')}>
              <MenuIcon name="warning" size="xs" />
              {$t('upload.dedupSkippedBadge')}
            </span>
          {/if}
        </div>
      </div>
    </div>

    <div class="upload-actions">
      {#if upload.status === 'failed'}
        <button
          class="upload-action-btn upload-retry-btn"
          on:click={handleRetry}
          title={$t('upload.retryUpload')}
          aria-label={$t('upload.retryUpload')}
        >
          <MenuIcon name="retry" size="xs" />
        </button>
      {/if}

      {#if upload.status === 'uploading' || upload.status === 'processing' || upload.status === 'preparing'}
        <button
          class="upload-action-btn upload-cancel-btn"
          on:click={handleCancel}
          title={$t('upload.cancelUpload')}
          aria-label={$t('upload.cancelUpload')}
        >
          <MenuIcon name="x" size="xs" />
        </button>
      {:else}
        <button
          class="upload-action-btn upload-remove-btn"
          on:click={handleRemove}
          title={$t('upload.removeFromList')}
          aria-label={$t('upload.removeFromList')}
        >
          <MenuIcon name="x" size="xs" />
        </button>
      {/if}
    </div>
  </div>

  {#if showProgress}
    <div class="progress-container">
      <div class="progress-bar">
        <div
          class="progress-fill"
          style="width: {upload.progress}%; background-color: {getStatusColor(upload.status)}"
        ></div>
      </div>
      <span class="progress-text">{upload.progress}%</span>
    </div>
  {/if}

  {#if upload.error}
    <div class="error-message" dir="auto">
      {upload.error}
    </div>
  {/if}
</div>

<style>
  .upload-item {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 8px;
    font-size: 0.875rem;
  }

  .upload-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .upload-info {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
    min-width: 0;
  }

  .upload-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    width: 20px;
    height: 20px;
  }

  .upload-details {
    flex: 1;
    min-width: 0;
  }

  .upload-name {
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 2px;
  }

  .upload-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 8px;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .upload-type {
    text-transform: uppercase;
    font-weight: 500;
    background: var(--accent-color);
    color: white;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .upload-dedup-skipped {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-weight: 500;
    color: #b45309;
    background: rgba(245, 158, 11, 0.12);
    border-radius: 4px;
    padding: 1px 6px;
    white-space: nowrap;
  }

  .upload-actions {
    display: flex;
    gap: 4px;
  }

  .upload-action-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    padding: 4px 6px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    line-height: 1;
    color: var(--text-secondary);
    transition: all 0.2s ease;
  }

  .upload-action-btn:hover {
    background: var(--hover-color);
  }

  .upload-retry-btn:hover {
    color: #10b981;
    background: rgba(16, 185, 129, 0.1);
  }

  .upload-cancel-btn:hover,
  .upload-remove-btn:hover {
    color: #ef4444;
    background: rgba(239, 68, 68, 0.1);
  }

  .progress-container {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
  }

  .progress-bar {
    flex: 1;
    height: 4px;
    background: var(--border-color);
    border-radius: 2px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    transition: width 0.3s ease;
  }

  .progress-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    min-width: 35px;
    text-align: end;
  }

  .error-message {
    margin-top: 6px;
    padding: 6px 8px;
    background: rgba(239, 68, 68, 0.1);
    border: 1px solid rgba(239, 68, 68, 0.2);
    border-radius: 4px;
    color: #ef4444;
    font-size: 0.75rem;
    line-height: 1.3;
  }

  /* Dark mode adjustments */
  :global([data-theme='dark']) .upload-item {
    background: var(--surface-color);
    border-color: var(--border-color);
  }

  :global([data-theme='dark']) .upload-name {
    color: var(--text-primary);
  }

  :global([data-theme='dark']) .upload-meta {
    color: var(--text-secondary);
  }

  :global([data-theme='dark']) .upload-dedup-skipped {
    color: #fcd34d;
    background: rgba(245, 158, 11, 0.18);
  }

  :global([data-theme='dark']) .upload-action-btn {
    color: var(--text-secondary);
  }

  :global([data-theme='dark']) .upload-action-btn:hover {
    background: var(--hover-color);
  }

  :global([data-theme='dark']) .progress-bar {
    background: var(--border-color);
  }

  :global([data-theme='dark']) .error-message {
    background: rgba(239, 68, 68, 0.15);
    border-color: rgba(239, 68, 68, 0.3);
    color: #fca5a5;
  }
</style>
