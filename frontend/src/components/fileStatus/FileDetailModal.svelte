<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t, locale } from '$stores/locale';
  import {
    formatDateTime,
    formatFileAge,
    formatProcessingTime,
    taskProgressPercent,
  } from '$lib/utils/formatting';
  import { translateFileStatus } from '$lib/fileStatus/statusLabel';
  import ProcessingProvenance from '$components/ProcessingProvenance.svelte';
  import type { DetailedFileStatus } from '$lib/types/fileStatus';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Badge from '$components/ui/Badge.svelte';
  import { badgeVariantForStatus } from '$lib/fileStatus/statusVariant';

  export let detailedStatus: DetailedFileStatus | null;
  export let selectedFile: string | null;
  export let retryingFiles: Set<string>;

  const dispatch = createEventDispatcher<{ close: void; retry: string }>();

  function closeModal() {
    dispatch('close');
  }
</script>

{#if detailedStatus && selectedFile}
  <BaseModal
    isOpen={true}
    title={`${$t('fileStatus.fileDetails')}: ${detailedStatus.file.filename}`}
    size="lg"
    onClose={closeModal}
  >
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
              <Badge variant={badgeVariantForStatus(detailedStatus.file.status_badge_class)} size="md" class="status-badge">
                {translateFileStatus($t, detailedStatus.file.status, detailedStatus.file.display_status)}
              </Badge>
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
              <span class="metadata-value">{formatDateTime(detailedStatus.file.upload_time, $t('common.notAvailable'), $locale)}</span>
            </div>
            {#if detailedStatus.file.completed_at}
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.completedAt')}:</span>
                <span class="metadata-value">{formatDateTime(detailedStatus.file.completed_at, $t('common.notAvailable'), $locale)}</span>
              </div>
            {/if}
            <div class="metadata-item">
              <span class="metadata-label">{$t('fileStatus.fileAge')}:</span>
              <span class="metadata-value">{formatFileAge(detailedStatus.file.upload_time, $locale) || detailedStatus.file.formatted_file_age || $t('fileStatus.unknown')}</span>
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

          {#if detailedStatus.file.processing_steps?.length}
            <div class="processing-provenance">
              <h4>{$t('fileProcessing.title')}</h4>
              <ProcessingProvenance steps={detailedStatus.file.processing_steps} />
            </div>
          {/if}

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
              {#each detailedStatus.task_details as task (task.id)}
                <div class="task-metadata-card">
                  <div class="task-card-header">
                    <span class="task-type-label">{task.task_type}</span>
                    <Badge variant={badgeVariantForStatus(task.status_badge_class)} size="md" class="status-badge">{translateFileStatus($t, task.status, task.status)}</Badge>
                  </div>
                  <div class="task-metadata-items">
                    <div class="metadata-item">
                      <span class="metadata-label">{$t('fileStatus.taskCreated')}</span>
                      <span class="metadata-value">{formatDateTime(task.created_at, $t('common.notAvailable'), $locale)}</span>
                    </div>
                    {#if task.updated_at}
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.lastUpdated')}</span>
                        <span class="metadata-value">{formatDateTime(task.updated_at, $t('common.notAvailable'), $locale)}</span>
                      </div>
                    {/if}
                    {#if task.completed_at}
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.taskCompleted')}</span>
                        <span class="metadata-value">{formatDateTime(task.completed_at, $t('common.notAvailable'), $locale)}</span>
                      </div>
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.processingTime')}</span>
                        <span class="metadata-value">{formatProcessingTime(task.created_at, task.completed_at, $locale) || task.formatted_processing_time || $t('common.unknown')}</span>
                      </div>
                    {/if}
                    <!-- `progress !== undefined` let `null` through: `null !== undefined`
                         is true and `Math.round(null * 100)` renders a confident "0%".
                         Require an actual finite number. -->
                    {#if typeof task.progress === 'number' && Number.isFinite(task.progress) && task.status === 'in_progress'}
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.progress')}</span>
                        <span class="metadata-value" dir="ltr">{taskProgressPercent(task.progress)}%</span>
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
  </BaseModal>
{/if}

<style>
  .file-details h4 {
    margin: 0 0 var(--space-4) 0;
    color: var(--text-color);
    font-weight: var(--font-weight-semibold);
  }

  .processing-provenance {
    margin-bottom: var(--space-4);
  }

  .metadata-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: var(--space-3);
    margin-bottom: var(--space-6);
  }

  .metadata-item {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .metadata-label {
    font-size: 0.8rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary-color);
  }

  .metadata-value {
    font-size: 0.9rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    overflow-wrap: anywhere;
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
    font-weight: var(--font-weight-medium);
    border-radius: var(--radius-sm);
    background-color: rgba(var(--warning-color-rgb), 0.15);
    color: var(--warning-text);
    border: 1px solid rgba(var(--warning-color-rgb), 0.3);
    vertical-align: middle;
    cursor: help;
  }

  .task-metadata-grid {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .task-metadata-card {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    padding: var(--space-4);
  }

  .task-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-4);
    padding-bottom: var(--space-2);
    border-bottom: 1px solid var(--border-color);
  }

  .task-type-label {
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    text-transform: capitalize;
  }

  .task-metadata-items {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--space-3);
  }

  .task-error-details {
    margin-top: var(--space-4);
    padding: var(--space-3);
    background: rgba(var(--error-color-rgb), 0.05);
    border-radius: var(--radius-sm);
    border: 1px solid rgba(var(--error-color-rgb), 0.2);
  }

  .task-error-details .metadata-label {
    color: var(--error-text);
  }

  .task-error {
    color: var(--error-text);
    font-size: var(--font-size-md);
    margin-top: var(--space-1);
  }

  .task-error-details .task-error {
    margin-top: var(--space-2);
    font-family: monospace;
    white-space: pre-wrap;
    font-size: 0.85rem;
    color: var(--error-text);
  }

  .warning {
    background: var(--warning-background);
    color: var(--warning-text);
    padding: var(--space-3);
    border-radius: var(--radius-sm);
    margin: var(--space-4) 0;
    border: 1px solid var(--warning-border);
  }

  :global([data-theme='dark']) .warning {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
    border-color: rgba(245, 158, 11, 0.3);
  }

  .retry-section {
    text-align: center;
    margin: var(--space-4) 0;
  }

  .retry-btn {
    padding: var(--space-1) var(--space-3);
    font-size: var(--font-size-md);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--duration-base);
    background: var(--success-solid);
    color: var(--text-on-solid);
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
    padding: var(--space-3) var(--space-6);
    font-size: var(--font-size-base);
  }

  .task-details {
    margin-top: var(--space-6);
    border-top: 1px solid var(--border-color);
    padding-top: var(--space-6);
  }

  .task-details h4 {
    color: var(--text-color);
    margin: 0 0 var(--space-4) 0;
  }
</style>
