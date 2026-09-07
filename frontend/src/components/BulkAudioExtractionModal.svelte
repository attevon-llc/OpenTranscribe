<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import {
    formatFileSize,
    calculateCompressionRatio,
    estimateAudioSize,
    estimateDurationFromFileSize,
  } from '$lib/utils/metadataMapper';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import { DEFAULT_EXTRACTION_CONFIG } from '$lib/types/audioExtraction';
  import { t } from '$stores/locale';
  import BaseModal from './ui/BaseModal.svelte';

  export let isOpen = false;
  export let videoFiles: File[] = [];
  export let regularFiles: File[] = [];

  const dispatch = createEventDispatcher<{
    confirmExtraction: void;
    uploadAllFull: void;
    cancel: void;
  }>();

  // Calculate total sizes
  $: totalVideoSize = videoFiles.reduce((sum, file) => sum + file.size, 0);
  $: estimatedTotalAudioSize = videoFiles.reduce((sum, file) => {
    const estimatedDuration = estimateDurationFromFileSize(file.size);
    return sum + estimateAudioSize(estimatedDuration, DEFAULT_EXTRACTION_CONFIG.bitrate);
  }, 0);
  $: compressionRatio = calculateCompressionRatio(totalVideoSize, estimatedTotalAudioSize);

  function handleExtractAudio() {
    dispatch('confirmExtraction');
    isOpen = false;
  }

  function handleUploadAllFull() {
    dispatch('uploadAllFull');
    isOpen = false;
  }

  function handleCancel() {
    // Close modal and clear file selection
    dispatch('cancel');
    isOpen = false;
  }
</script>

<BaseModal isOpen={isOpen && videoFiles.length > 0} title={$t('extraction.largeVideosDetected')} onClose={handleCancel} maxWidth="600px">
  <div class="info-section">
    <div class="info-icon">
      <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M9 18V5l12-2v13"></path>
        <circle cx="6" cy="18" r="3"></circle>
        <circle cx="18" cy="16" r="3"></circle>
      </svg>
    </div>
    <!-- extraction.bulkMessage carries a <strong>; rendered as text it leaked the
         literal tags into the UI. Same treatment as UploadStepExtraction: {@html}
         through the DOMPurify allowlist. -->
    <p class="info-message">
      {@html sanitizeHighlightHtml($t('extraction.bulkMessage', { count: videoFiles.length }))}
    </p>
  </div>

  <!-- File List -->
  <div class="file-list-container">
    <div class="file-list-header">{$t('extraction.filesToExtract')}</div>
    <div class="file-list">
      {#each videoFiles as file}
        <div class="file-item">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="23 7 16 12 23 17 23 7"></polygon>
            <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
          </svg>
          <span class="file-name">{file.name}</span>
          <span class="file-size">{formatFileSize(file.size)}</span>
        </div>
      {/each}
    </div>
  </div>

  {#if regularFiles.length > 0}
    <div class="regular-files-notice">
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="12" y1="16" x2="12" y2="12"></line>
        <line x1="12" y1="8" x2="12.01" y2="8"></line>
      </svg>
      <span>{$t('extraction.otherFilesNormal', { count: regularFiles.length })}</span>
    </div>
  {/if}

  <!-- Size comparison -->
  <div class="comparison-section">
    <div class="comparison-item">
      <div class="comparison-label">{$t('extraction.totalVideoSize')}</div>
      <div class="comparison-value video">{formatFileSize(totalVideoSize)}</div>
    </div>
    <div class="comparison-arrow">→</div>
    <div class="comparison-item">
      <div class="comparison-label">{$t('extraction.estimatedAudioSize')}</div>
      <div class="comparison-value audio">{formatFileSize(estimatedTotalAudioSize)}</div>
    </div>
  </div>

  <div class="savings-badge">
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="20 6 9 17 4 12"></polyline>
    </svg>
    <span>{$t('extraction.bulkSavingsBadge', { ratio: compressionRatio })}</span>
  </div>

  <svelte:fragment slot="footer">
    <button
      class="modal-button secondary-button"
      on:click={handleUploadAllFull}
      type="button"
    >
      {$t('extraction.uploadAllAsVideo')}
    </button>
    <button
      class="modal-button primary-button"
      on:click={handleExtractAudio}
      type="button"
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M9 18V5l12-2v13"></path>
        <circle cx="6" cy="18" r="3"></circle>
        <circle cx="18" cy="16" r="3"></circle>
      </svg>
      {$t('extraction.extractAudioCount', { count: videoFiles.length })}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .info-section {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    margin-bottom: 1.5rem;
  }

  .info-icon {
    color: var(--primary-on-surface);
    margin-bottom: 1rem;
  }

  .info-message {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.6;
    font-size: 0.95rem;
  }

  .file-list-container {
    margin: 1.5rem 0;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
  }

  .file-list-header {
    padding: 0.75rem 1rem;
    background: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
    font-weight: 500;
    font-size: 0.875rem;
    color: var(--text-color);
  }

  .file-list {
    max-height: 200px;
    overflow-y: auto;
    background: var(--background-color);
  }

  .file-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border-color);
    transition: background 0.15s;
  }

  .file-item:last-child {
    border-bottom: none;
  }

  .file-item:hover {
    background: var(--surface-color);
  }

  .file-item svg {
    flex-shrink: 0;
    color: var(--primary-on-surface);
  }

  .file-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-color);
    font-size: 0.875rem;
  }

  .file-size {
    flex-shrink: 0;
    color: var(--text-secondary);
    font-size: 0.75rem;
    font-weight: 500;
  }

  .regular-files-notice {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    background: rgba(59, 130, 246, 0.05);
    border: 1px solid rgba(59, 130, 246, 0.2);
    border-radius: 6px;
    color: var(--text-secondary);
    font-size: 0.875rem;
    margin-bottom: 1rem;
  }

  .regular-files-notice svg {
    flex-shrink: 0;
    color: var(--primary-on-surface);
  }

  .comparison-section {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 1rem;
    margin: 1.5rem 0;
    padding: 1rem;
    background: var(--surface-color);
    border-radius: 8px;
    border: 1px solid var(--border-color);
  }

  .comparison-item {
    flex: 1;
    text-align: center;
  }

  .comparison-label {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-bottom: 0.5rem;
    text-transform: uppercase;
    font-weight: 500;
  }

  .comparison-value {
    font-size: 1.1rem;
    font-weight: 600;
    padding: 0.5rem;
    border-radius: 6px;
  }

  .comparison-value.video {
    color: #ef4444;
    background: rgba(239, 68, 68, 0.1);
  }

  .comparison-value.audio {
    color: #10b981;
    background: rgba(16, 185, 129, 0.1);
  }

  .comparison-arrow {
    color: var(--text-secondary);
    font-size: 1.5rem;
  }

  .savings-badge {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    background: rgba(16, 185, 129, 0.1);
    border: 1px solid rgba(16, 185, 129, 0.2);
    border-radius: 8px;
    color: #10b981;
    font-weight: 500;
    font-size: 0.875rem;
  }

  .modal-button {
    padding: 0.6rem 1.2rem;
    border: none;
    border-radius: 10px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    min-width: 140px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
  }

  .secondary-button {
    background: var(--card-background);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
  }

  .secondary-button:hover {
    background: var(--button-hover);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.08);
  }

  .secondary-button:active {
    transform: scale(1);
  }

  .primary-button {
    background: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .primary-button:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb, 59, 130, 246), 0.3);
  }

  .primary-button:active {
    transform: scale(1);
  }

  /* Responsive design */
  @media (max-width: 768px) {
    .modal-button {
      width: 100%;
      min-height: 44px;
    }

    .comparison-section {
      flex-direction: column;
      gap: 0.5rem;
    }

    .comparison-arrow {
      transform: rotate(90deg);
    }
  }

  /* Reduced motion support */
  @media (prefers-reduced-motion: reduce) {
    .modal-button {
      transition: none;
    }
  }
</style>
