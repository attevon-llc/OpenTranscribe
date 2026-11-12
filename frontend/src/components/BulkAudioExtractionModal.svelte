<script lang="ts">
  import InfoIcon from '$components/ui/InfoIcon.svelte';
  import SanitizedHtml from '$components/ui/SanitizedHtml.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { createEventDispatcher } from 'svelte';
  import {
    calculateCompressionRatio,
    estimateAudioSize,
    estimateDurationFromFileSize,
  } from '$lib/utils/metadataMapper';
  import { formatFileSize } from '$lib/utils/formatting';
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

<BaseModal isOpen={isOpen && videoFiles.length > 0} title={$t('extraction.largeVideosDetected')} onClose={handleCancel} size="md">
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
      <SanitizedHtml html={$t('extraction.bulkMessage', { count: videoFiles.length })} />
    </p>
  </div>

  <!-- File List -->
  <div class="file-list-container">
    <div class="file-list-header">{$t('extraction.filesToExtract')}</div>
    <div class="file-list">
      {#each videoFiles as file, i (i)}
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
      <InfoIcon />
      <span>{$t('extraction.otherFilesNormal', { count: regularFiles.length })}</span>
    </div>
  {/if}

  <!-- Size comparison -->
  <div class="comparison-section">
    <div class="comparison-item">
      <div class="comparison-label">{$t('extraction.totalVideoSize')}</div>
      <div class="comparison-value video">{formatFileSize(totalVideoSize)}</div>
    </div>
    <div class="comparison-arrow" aria-hidden="true"><MenuIcon name="arrow-right" size="lg" /></div>
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
      class="btn btn-lg btn-secondary bulk-extract-button"
      on:click={handleUploadAllFull}
      type="button"
    >
      {$t('extraction.uploadAllAsVideo')}
    </button>
    <button
      class="btn btn-lg btn-primary bulk-extract-button"
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
    margin-bottom: var(--space-6);
  }

  .info-icon {
    color: var(--primary-on-surface);
    margin-bottom: var(--space-4);
  }

  .info-message {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.6;
    font-size: var(--font-size-base);
  }

  .file-list-container {
    margin: var(--space-6) 0;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  .file-list-header {
    padding: var(--space-3) var(--space-4);
    background: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-md);
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
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--border-color);
    transition: background var(--duration-fast);
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
    font-size: var(--font-size-md);
  }

  .file-size {
    flex-shrink: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
  }

  .regular-files-notice {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    background: rgba(var(--accent-rgb), 0.05);
    border: 1px solid rgba(var(--accent-rgb), 0.2);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-md);
    margin-bottom: var(--space-4);
  }

  .regular-files-notice :global(svg) {
    flex-shrink: 0;
    color: var(--primary-on-surface);
  }

  .comparison-section {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-4);
    margin: var(--space-6) 0;
    padding: var(--space-4);
    background: var(--surface-color);
    border-radius: var(--radius-lg);
    border: 1px solid var(--border-color);
  }

  .comparison-item {
    flex: 1;
    text-align: center;
  }

  .comparison-label {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-bottom: var(--space-2);
    font-weight: var(--font-weight-medium);
  }

  .comparison-value {
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-semibold);
    padding: var(--space-2);
    border-radius: var(--radius-md);
  }

  .comparison-value.video {
    color: var(--error-text);
    background: var(--error-bg);
  }

  .comparison-value.audio {
    color: var(--success-text);
    background: var(--success-bg);
  }

  .comparison-arrow {
    display: inline-flex;
    align-items: center;
    color: var(--text-secondary);
  }

  .savings-badge {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    background: var(--success-bg);
    border: 1px solid rgba(var(--success-color-rgb), 0.2);
    border-radius: var(--radius-lg);
    color: var(--success-text);
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-md);
  }

  .bulk-extract-button {
    min-width: 140px;
  }

  /* Responsive design */
  @media (max-width: 768px) {
    .bulk-extract-button {
      width: 100%;
      min-height: 44px;
    }

    .comparison-section {
      flex-direction: column;
      gap: var(--space-2);
    }

    .comparison-arrow {
      transform: rotate(90deg);
    }

    /* The glyph is already mirrored in RTL; a quarter turn the other way still points down. */
    :global([dir='rtl']) .comparison-arrow {
      transform: rotate(-90deg);
    }
  }
</style>
