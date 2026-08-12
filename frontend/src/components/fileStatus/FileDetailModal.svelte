<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '../../stores/locale';
  import { taskProgressPercent } from '$lib/utils/formatting';

  export let detailedStatus: any;
  export let selectedFile: any;
  export let retryingFiles: Set<any>;

  const dispatch = createEventDispatcher<{ close: void; retry: any }>();

  function closeModal() {
    dispatch('close');
  }

  // Helper function to translate status values
  function translateStatus(status: string): string {
    const statusMap: Record<string, string> = {
      'completed': $t('common.completed'),
      'processing': $t('common.processing'),
      'pending': $t('common.pending'),
      'error': $t('common.error'),
      'failed': $t('fileStatus.failed'),
      'in_progress': $t('fileStatus.inProgress'),
      'Completed': $t('common.completed'),
      'Processing': $t('common.processing'),
      'Pending': $t('common.pending'),
      'Error': $t('common.error'),
      'Failed': $t('fileStatus.failed'),
      'In Progress': $t('fileStatus.inProgress'),
    };
    return statusMap[status] || status;
  }

  function formatDate(dateString: any) {
    if (!dateString) return $t('common.notAvailable');

    const date = new Date(dateString);
    return new Intl.DateTimeFormat('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    }).format(date);
  }
</script>

{#if detailedStatus && selectedFile}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div
    class="detailed-status-modal"
    role="presentation"
    on:click={closeModal}
    on:wheel|preventDefault|self
    on:touchmove|preventDefault|self
    on:keydown={(e) => e.key === 'Escape' && closeModal()}
  >
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <!-- svelte-ignore a11y_interactive_supports_focus -->
    <div
      class="modal-content"
      role="dialog"
      aria-modal="true"
      on:click|stopPropagation
      on:keydown|stopPropagation
    >
      <div class="modal-header">
        <h3>{$t('fileStatus.fileDetails')}: {detailedStatus.file.filename}</h3>
        <button class="close-btn" on:click={closeModal}>×</button>
      </div>

      <div class="modal-body">
        <!-- File Details Grid -->
        <div class="file-details">
          <h4>{$t('fileStatus.fileInformation')}</h4>
          <div class="metadata-grid">
            <div class="metadata-item">
              <span class="metadata-label">{$t('fileStatus.fileName')}:</span>
              <span class="metadata-value">{detailedStatus.file.filename}</span>
            </div>
            <div class="metadata-item">
              <span class="metadata-label">{$t('common.status')}:</span>
              <span class="status-badge {detailedStatus.file.status_badge_class || 'status-unknown'}">
                {translateStatus(detailedStatus.file.display_status || detailedStatus.file.status)}
              </span>
            </div>
            <div class="metadata-item">
              <span class="metadata-label">{$t('fileStatus.fileSize')}:</span>
              <span class="metadata-value">{detailedStatus.file.formatted_file_size || $t('fileStatus.unknown')}</span>
            </div>
            <div class="metadata-item">
              <span class="metadata-label">{$t('common.duration')}:</span>
              <span class="metadata-value">{detailedStatus.file.formatted_duration || $t('fileStatus.unknown')}</span>
            </div>
            <div class="metadata-item">
              <span class="metadata-label">{$t('fileStatus.language')}:</span>
              <span class="metadata-value">{detailedStatus.file.language || $t('fileStatus.autoDetected')}</span>
            </div>
            <div class="metadata-item">
              <span class="metadata-label">{$t('fileStatus.uploadTime')}:</span>
              <span class="metadata-value">{formatDate(detailedStatus.file.upload_time)}</span>
            </div>
            {#if detailedStatus.file.completed_at}
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.completedAt')}:</span>
                <span class="metadata-value">{formatDate(detailedStatus.file.completed_at)}</span>
              </div>
            {/if}
            <div class="metadata-item">
              <span class="metadata-label">{$t('fileStatus.fileAge')}:</span>
              <span class="metadata-value">{detailedStatus.file.formatted_file_age || $t('fileStatus.unknown')}</span>
            </div>
            {#if detailedStatus.file.whisper_model}
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileDetail.whisperModel')}:</span>
                <span class="metadata-value model-name-value">
                  {detailedStatus.file.whisper_model}
                  {#if detailedStatus.file.model_fallback_occurred}
                    <span class="fallback-badge" title="{$t('fileDetail.requestedModel')}: {detailedStatus.file.requested_whisper_model}">
                      {$t('fileDetail.modelFallback')}
                    </span>
                  {/if}
                </span>
              </div>
            {/if}
            {#if detailedStatus.file.diarization_disabled}
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.diarizationLabel')}</span>
                <span class="metadata-value diarization-disabled-value">{$t('metadata.diarizationDisabled')}</span>
              </div>
            {:else if detailedStatus.file.diarization_model}
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.diarizationLabel')}</span>
                <span class="metadata-value model-name-value">{detailedStatus.file.diarization_model}</span>
              </div>
            {/if}
          </div>

          {#if detailedStatus.is_stuck}
            <div class="warning">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display: inline; margin-right: 4px;">
                <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>
                <path d="M12 9v4"/>
                <path d="m12 17 .01 0"/>
              </svg>
              {$t('fileStatus.fileStuck')}
            </div>
          {/if}

          {#if detailedStatus.can_retry}
            <div class="retry-section">
              <button
                class="retry-btn large"
                on:click={() => dispatch('retry', selectedFile)}
                disabled={retryingFiles.has(selectedFile)}
              >
                {retryingFiles.has(selectedFile) ? $t('fileStatus.retrying') : $t('fileStatus.retryProcessing')}
              </button>
            </div>
          {/if}
        </div>

        {#if detailedStatus.task_details.length > 0}
          <div class="task-details">
            <h4>{$t('fileStatus.taskDetailsTitle')}</h4>
            <div class="task-metadata-grid">
              {#each detailedStatus.task_details as task}
                <div class="task-metadata-card">
                  <div class="task-card-header">
                    <span class="task-type-label">{task.task_type}</span>
                    <span class="status-badge {task.status_badge_class || 'status-unknown'}">{translateStatus(task.status)}</span>
                  </div>
                  <div class="task-metadata-items">
                    <div class="metadata-item">
                      <span class="metadata-label">{$t('fileStatus.taskCreated')}</span>
                      <span class="metadata-value">{formatDate(task.created_at)}</span>
                    </div>
                    {#if task.updated_at}
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.lastUpdated')}</span>
                        <span class="metadata-value">{formatDate(task.updated_at)}</span>
                      </div>
                    {/if}
                    {#if task.completed_at}
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.taskCompleted')}</span>
                        <span class="metadata-value">{formatDate(task.completed_at)}</span>
                      </div>
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.processingTime')}</span>
                        <span class="metadata-value">{task.formatted_processing_time || $t('common.unknown')}</span>
                      </div>
                    {/if}
                    <!-- `progress !== undefined` let `null` through: `null !== undefined`
                         is true and `Math.round(null * 100)` renders a confident "0%".
                         Require an actual finite number. -->
                    {#if typeof task.progress === 'number' && Number.isFinite(task.progress) && task.status === 'in_progress'}
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.progress')}</span>
                        <span class="metadata-value">{taskProgressPercent(task.progress)}%</span>
                      </div>
                    {/if}
                    {#if task.whisper_model}
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.whisperModel')}</span>
                        <span class="metadata-value model-name-value">{task.whisper_model}</span>
                      </div>
                    {/if}
                    {#if task.diarization_model}
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.diarization')}</span>
                        <span class="metadata-value model-name-value">{task.diarization_model}</span>
                      </div>
                    {/if}
                  </div>
                  {#if task.error_message}
                    <div class="task-error-details">
                      <span class="metadata-label">{$t('fileStatus.errorLabel')}</span>
                      <div class="task-error">{task.error_message}</div>
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .status-badge {
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 500;
    text-transform: uppercase;
  }

  .status-completed {
    background: rgba(16, 185, 129, 0.1);
    color: #10b981;
  }

  .status-processing {
    background: rgba(59, 130, 246, 0.1);
    color: var(--primary-color);
  }

  .status-pending {
    background: rgba(245, 158, 11, 0.1);
    color: #f59e0b;
  }

  .status-error {
    background: rgba(239, 68, 68, 0.1);
    color: #ef4444;
  }

  .status-unknown {
    background: rgba(156, 163, 175, 0.1);
    color: #6b7280;
  }

  :global(.dark) .status-completed {
    background: rgba(16, 185, 129, 0.2);
    color: #34d399;
  }

  :global(.dark) .status-processing {
    background: rgba(59, 130, 246, 0.2);
    color: #60a5fa;
  }

  :global(.dark) .status-pending {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  :global(.dark) .status-error {
    background: rgba(239, 68, 68, 0.2);
    color: #f87171;
  }

  .detailed-status-modal {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    overflow: hidden;
    overscroll-behavior: none;
  }

  :global(.dark) .detailed-status-modal {
    background: rgba(0, 0, 0, 0.7);
  }

  .modal-content {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    max-width: 600px;
    width: 90%;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
  }

  :global(.dark) .modal-content {
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3), 0 10px 10px -5px rgba(0, 0, 0, 0.2);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .modal-header h3 {
    margin: 0;
    color: var(--text-color);
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 1.5rem;
    cursor: pointer;
    color: var(--text-light);
    transition: color 0.2s ease;
  }

  .close-btn:hover {
    color: var(--text-color);
  }

  .modal-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }

  .file-details h4 {
    margin: 0 0 1rem 0;
    color: var(--text-color);
    font-weight: 600;
  }

  .metadata-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }

  .metadata-item {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .metadata-label {
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--text-secondary-color);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .metadata-value {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-color);
    word-break: break-word;
  }

  .model-name-value {
    font-family: 'SF Mono', 'Fira Code', 'Fira Mono', 'Roboto Mono', monospace;
    font-size: 0.85rem;
  }

  .diarization-disabled-value {
    font-style: italic;
    color: var(--text-secondary);
  }

  .fallback-badge {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0.1rem 0.4rem;
    font-size: 0.7rem;
    font-family: inherit;
    font-weight: 500;
    border-radius: 4px;
    background-color: rgba(var(--warning-color-rgb, 217, 119, 6), 0.15);
    color: var(--warning-color, #d97706);
    border: 1px solid rgba(var(--warning-color-rgb, 217, 119, 6), 0.3);
    vertical-align: middle;
    cursor: help;
  }

  .task-metadata-grid {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .task-metadata-card {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 1rem;
  }

  .task-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .task-type-label {
    font-weight: 600;
    color: var(--text-color);
    text-transform: capitalize;
  }

  .task-metadata-items {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 0.75rem;
  }

  .task-error-details {
    margin-top: 1rem;
    padding: 0.75rem;
    background: rgba(var(--error-color-rgb, 239, 68, 68), 0.05);
    border-radius: 4px;
    border: 1px solid rgba(var(--error-color-rgb, 239, 68, 68), 0.2);
  }

  .task-error-details .metadata-label {
    color: var(--error-color);
  }

  .task-error-details .task-error {
    margin-top: 0.5rem;
    font-family: monospace;
    white-space: pre-wrap;
    font-size: 0.85rem;
    color: var(--error-color);
  }

  .warning {
    background: var(--warning-background);
    color: var(--warning-text);
    padding: 0.75rem;
    border-radius: 4px;
    margin: 1rem 0;
    border: 1px solid var(--warning-border);
  }

  :global(.dark) .warning {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
    border-color: rgba(245, 158, 11, 0.3);
  }

  .retry-section {
    text-align: center;
    margin: 1rem 0;
  }

  .retry-btn {
    padding: 0.25rem 0.75rem;
    font-size: 0.875rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
    background: var(--success-color);
    color: white;
    border-color: var(--success-color);
  }

  .retry-btn:hover {
    background: var(--success-hover);
    border-color: var(--success-hover);
    transform: scale(1.02);
  }

  .retry-btn:disabled {
    background: var(--text-light);
    border-color: var(--text-light);
    cursor: not-allowed;
    transform: none;
  }

  .retry-btn.large {
    padding: 0.75rem 1.5rem;
    font-size: 1rem;
  }

  .task-details {
    margin-top: 1.5rem;
    border-top: 1px solid var(--border-color);
    padding-top: 1.5rem;
  }

  .task-details h4 {
    color: var(--text-color);
    margin: 0 0 1rem 0;
  }

  .task-error {
    color: var(--error-color);
    font-size: 0.875rem;
    margin-top: 0.25rem;
  }
</style>
