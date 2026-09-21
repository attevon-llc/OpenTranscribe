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

  /**
   * Status -> CSS class, keyed to theme tokens rather than hardcoded hex
   * (#752 gap 2). Each token pair was chosen because it clears BOTH the WCAG
   * AA 4.5:1 text threshold (the status icon is a glyph, i.e. text) and the
   * WCAG 2.1 SC 1.4.11 3:1 non-text threshold (the progress-bar fill against
   * --border-color) in both themes — verified in
   * `styles/primary-contrast.test.ts`. The plain `--success-color` /
   * `--warning-color` / etc. tokens do NOT clear 3:1 against --border-color
   * in light mode (measured as low as 1.73:1 for warning), so this
   * deliberately reuses the existing darker "-text"/"-dark" tokens instead
   * of the base status colors.
   */
  function statusClass(status: string): string {
    switch (status) {
      case 'completed': return 'status-completed';
      case 'failed': return 'status-failed';
      case 'cancelled': return 'status-cancelled';
      case 'uploading':
      case 'processing':
      case 'preparing': return 'status-active';
      default: return 'status-pending';
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
      <div class="upload-icon {statusClass(upload.status)}">
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
          class="upload-tray-action-btn upload-tray-retry-btn"
          on:click={handleRetry}
          title={$t('upload.retryUpload')}
        >
          ↻
        </button>
      {/if}

      {#if upload.status === 'uploading' || upload.status === 'processing' || upload.status === 'preparing'}
        <button
          class="upload-tray-action-btn upload-tray-cancel-btn"
          on:click={handleCancel}
          title={$t('upload.cancelUpload')}
        >
          ✗
        </button>
      {:else}
        <button
          class="upload-tray-action-btn upload-tray-remove-btn"
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
      <div class="upload-tray-progress-bar">
        <div
          class="progress-fill {statusClass(upload.status)}"
          style="width: {upload.progress}%"
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

  /* Status colours (#752 gap 2) — one token per status, shared by the icon
     (text, needs WCAG AA 4.5:1) and the progress-fill (non-text, needs WCAG
     2.1 SC 1.4.11's 3:1) against their respective backgrounds. Each token
     already carries its own [data-theme='dark'] value in theme.css, so no
     separate dark-mode override is needed here. */
  .upload-icon.status-completed { color: var(--color-success-text); }
  .upload-icon.status-failed { color: var(--error-dark); }
  .upload-icon.status-cancelled { color: var(--text-secondary); }
  .upload-icon.status-active { color: var(--color-info-text); }
  .upload-icon.status-pending { color: var(--color-warning-text); }

  .progress-fill.status-completed { background-color: var(--color-success-text); }
  .progress-fill.status-failed { background-color: var(--error-dark); }
  .progress-fill.status-cancelled { background-color: var(--text-secondary); }
  .progress-fill.status-active { background-color: var(--color-info-text); }
  .progress-fill.status-pending { background-color: var(--color-warning-text); }

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

  .upload-tray-action-btn {
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

  .upload-tray-action-btn:hover {
    background: var(--hover-color);
  }

  .upload-tray-retry-btn:hover {
    color: #10b981;
    background: rgba(16, 185, 129, 0.1);
  }

  .upload-tray-cancel-btn:hover,
  .upload-tray-remove-btn:hover {
    color: #ef4444;
    background: rgba(239, 68, 68, 0.1);
  }

  .progress-container {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
  }

  .upload-tray-progress-bar {
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

  :global([data-theme='dark']) .upload-tray-action-btn {
    color: var(--text-secondary);
  }

  :global([data-theme='dark']) .upload-tray-action-btn:hover {
    background: var(--hover-color);
  }

  :global([data-theme='dark']) .upload-tray-progress-bar {
    background: var(--border-color);
  }

  :global([data-theme='dark']) .error-message {
    background: rgba(239, 68, 68, 0.15);
    border-color: rgba(239, 68, 68, 0.3);
    color: #fca5a5;
  }
</style>
