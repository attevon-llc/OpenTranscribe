<script lang="ts">
  import type { UploadItem } from '../lib/services/uploadService';
  import { uploadsStore } from '../stores/uploads';
  import { t } from '$stores/locale';

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
      case 'completed': return '#10b981';
      case 'failed': return '#ef4444';
      case 'cancelled': return '#6b7280';
      case 'uploading':
      case 'processing':
      case 'preparing': return '#3b82f6';
      default: return '#f59e0b';
    }
  }

  // Get status icon
  function getStatusIcon(status: string): string {
    switch (status) {
      case 'completed': return '✓';
      case 'failed': return '✗';
      case 'cancelled': return '⊘';
      case 'uploading':
      case 'processing': return '↑';
      case 'preparing': return '⚙';
      default: return '⏳';
    }
  }

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
      <div class="upload-icon" style="color: {getStatusColor(upload.status)}">
        {getStatusIcon(upload.status)}
      </div>
      <div class="upload-details">
        <div class="upload-name" title={upload.name}>
          {upload.name}
        </div>
        <div class="upload-meta">
          <span class="upload-type">{upload.type}</span>
          {#if upload.size}
            <span class="upload-size">{formatFileSize(upload.size)}</span>
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
              ⚠ {$t('upload.dedupSkippedBadge')}
            </span>
          {/if}
        </div>
      </div>
    </div>

    <div class="upload-actions">
      {#if upload.status === 'failed'}
        <button
          class="action-btn retry-btn"
          on:click={handleRetry}
          title={$t('upload.retryUpload')}
        >
          ↻
        </button>
      {/if}

      {#if upload.status === 'uploading' || upload.status === 'processing' || upload.status === 'preparing'}
        <button
          class="action-btn cancel-btn"
          on:click={handleCancel}
          title={$t('upload.cancelUpload')}
        >
          ✗
        </button>
      {:else}
        <button
          class="action-btn remove-btn"
          on:click={handleRemove}
          title={$t('upload.removeFromList')}
        >
          ✗
        </button>
      {/if}
    </div>
  </div>

  {#if upload.status === 'uploading' || upload.status === 'processing' || upload.status === 'preparing'}
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
    <div class="error-message">
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
    font-size: 16px;
    font-weight: bold;
    min-width: 20px;
    text-align: center;
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
    gap: 8px;
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
  }

  .upload-dedup-skipped {
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

  .action-btn {
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

  .action-btn:hover {
    background: var(--hover-color);
  }

  .retry-btn:hover {
    color: #10b981;
    background: rgba(16, 185, 129, 0.1);
  }

  .cancel-btn:hover,
  .remove-btn:hover {
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
    text-align: right;
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
  :global(.dark) .upload-item {
    background: var(--surface-color);
    border-color: var(--border-color);
  }

  :global(.dark) .upload-name {
    color: var(--text-primary);
  }

  :global(.dark) .upload-meta {
    color: var(--text-secondary);
  }

  :global(.dark) .upload-dedup-skipped {
    color: #fcd34d;
    background: rgba(245, 158, 11, 0.18);
  }

  :global(.dark) .action-btn {
    color: var(--text-secondary);
  }

  :global(.dark) .action-btn:hover {
    background: var(--hover-color);
  }

  :global(.dark) .progress-bar {
    background: var(--border-color);
  }

  :global(.dark) .error-message {
    background: rgba(239, 68, 68, 0.15);
    border-color: rgba(239, 68, 68, 0.3);
    color: #fca5a5;
  }
</style>
