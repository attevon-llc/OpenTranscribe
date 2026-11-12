<script lang="ts">
  import type { UploadItem } from '$lib/services/uploadService';
  import { uploadsStore } from '$stores/uploads';
  import { t } from '$stores/locale';
  import { formatFileSize } from '$lib/utils/formatting';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import type { MenuIconName } from '$lib/ui/menuIcons';

  export let upload: UploadItem;

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
            <span class="upload-size" dir="ltr">{upload.size ? formatFileSize(upload.size) : ''}</span>
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
      <div class="progress-bar-slot">
        <ProgressBar
          percent={upload.progress}
          ariaLabel={upload.name}
          tone={upload.status === 'queued' ? 'warning' : 'primary'}
        />
      </div>
      <span class="progress-text" dir="ltr">{upload.progress}%</span>
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
    border-radius: var(--radius-lg);
    padding: var(--space-3);
    margin-bottom: var(--space-2);
    font-size: var(--font-size-md);
  }

  .upload-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .upload-info {
    display: flex;
    align-items: center;
    gap: var(--space-2-5);
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
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: var(--space-0-5);
  }

  .upload-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1) var(--space-2);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .upload-type {
    text-transform: uppercase;
    font-weight: var(--font-weight-medium);
    background: var(--primary-color);
    color: var(--text-on-solid);
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-2xs);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .upload-dedup-skipped {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-weight: var(--font-weight-medium);
    color: var(--warning-text);
    background: var(--warning-bg);
    border-radius: var(--radius-sm);
    padding: 1px var(--space-1-5);
    white-space: nowrap;
  }

  .upload-actions {
    display: flex;
    gap: var(--space-1);
  }

  .upload-action-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    padding: var(--space-1) var(--space-1-5);
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: var(--font-size-xs);
    line-height: 1;
    color: var(--text-secondary);
    transition: all var(--duration-base) ease;
  }

  .upload-action-btn:hover {
    background: var(--hover-color);
  }

  .upload-retry-btn:hover {
    color: var(--success-text);
    background: var(--success-bg);
  }

  .upload-cancel-btn:hover,
  .upload-remove-btn:hover {
    color: var(--error-text);
    background: var(--error-bg);
  }

  .progress-container {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  .progress-bar-slot {
    flex: 1;
  }

  .progress-text {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    min-width: 35px;
    text-align: end;
  }

  .error-message {
    margin-top: var(--space-1-5);
    padding: var(--space-1-5) var(--space-2);
    background: var(--error-bg);
    border: 1px solid var(--error-border);
    border-radius: var(--radius-sm);
    color: var(--error-text);
    font-size: var(--font-size-xs);
    line-height: 1.3;
  }
</style>
