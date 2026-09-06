<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import {
    formatFileSize,
    calculateCompressionRatio,
    estimateAudioSize,
    estimateDurationFromFileSize,
  } from '$lib/utils/metadataMapper';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import { DEFAULT_EXTRACTION_CONFIG } from '$lib/types/audioExtraction';

  export let file: File | null = null;
  export let choice: 'extract' | 'full' = 'extract';

  const dispatch = createEventDispatcher<{
    choiceChange: { choice: 'extract' | 'full' };
  }>();

  let estimatedAudioSize = 0;
  let compressionRatio = 0;

  $: if (file) {
    const estimatedDuration = estimateDurationFromFileSize(file.size);
    estimatedAudioSize = estimateAudioSize(estimatedDuration, DEFAULT_EXTRACTION_CONFIG.bitrate);
    compressionRatio = calculateCompressionRatio(file.size, estimatedAudioSize);
  }

  function selectChoice(value: 'extract' | 'full') {
    choice = value;
    dispatch('choiceChange', { choice });
  }
</script>

<div class="step-extraction">
  {#if file}
    <div class="info-section">
      <div class="info-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 18V5l12-2v13"></path>
          <circle cx="6" cy="18" r="3"></circle>
          <circle cx="18" cy="16" r="3"></circle>
        </svg>
      </div>
      <!-- The locale string carries a <strong>, so this has to be {@html}. i18next
           runs with interpolation.escapeValue=false, so the interpolated value is
           NOT escaped for us — sanitize the rendered result. -->
      <p class="info-message">
        {@html sanitizeHighlightHtml($t('extraction.videoSizeMessage', { size: formatFileSize(file.size) }))}
      </p>
    </div>

    <div class="comparison">
      <div class="comparison-item">
        <div class="comparison-label">{$t('extraction.videoFile')}</div>
        <div class="comparison-value video">{formatFileSize(file.size)}</div>
      </div>
      <div class="comparison-arrow">
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="5" y1="12" x2="19" y2="12"></line>
          <polyline points="12 5 19 12 12 19"></polyline>
        </svg>
      </div>
      <div class="comparison-item">
        <div class="comparison-label">{$t('extraction.audioOnly')}</div>
        <div class="comparison-value audio">{formatFileSize(estimatedAudioSize)}</div>
      </div>
    </div>

    <!-- Radio choice -->
    <div class="choice-group">
      <label class="choice-option" class:selected={choice === 'extract'}>
        <input type="radio" name="extraction-choice" value="extract" bind:group={choice} on:change={() => selectChoice('extract')} />
        <div class="choice-content">
          <div class="choice-header">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 18V5l12-2v13"></path>
              <circle cx="6" cy="18" r="3"></circle>
              <circle cx="18" cy="16" r="3"></circle>
            </svg>
            <span class="choice-title">{$t('extraction.extractAudio')}</span>
            <span class="choice-badge recommended">{$t('extraction.recommendedBadge')}</span>
          </div>
          <span class="choice-desc">{$t('extraction.extractSavings', { ratio: compressionRatio })}</span>
        </div>
      </label>

      <label class="choice-option" class:selected={choice === 'full'}>
        <input type="radio" name="extraction-choice" value="full" bind:group={choice} on:change={() => selectChoice('full')} />
        <div class="choice-content">
          <div class="choice-header">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="17 8 12 3 7 8"></polyline>
              <line x1="12" y1="3" x2="12" y2="15"></line>
            </svg>
            <span class="choice-title">{$t('extraction.uploadFullVideo')}</span>
          </div>
          <span class="choice-desc">{$t('extraction.uploadFullDesc', { size: formatFileSize(file.size) })}</span>
        </div>
      </label>
    </div>

    <div class="metadata-note">
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="12" y1="16" x2="12" y2="12"></line>
        <line x1="12" y1="8" x2="12.01" y2="8"></line>
      </svg>
      <span>{$t('extraction.metadataPreserved')}</span>
    </div>
  {/if}
</div>

<style>
  .step-extraction {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .info-section {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }

  .info-icon {
    color: var(--primary-on-surface);
    margin-bottom: 0.375rem;
  }

  .info-message {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.5;
    font-size: 0.8125rem;
  }

  .comparison {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
    padding: 0.625rem;
    background: var(--surface-color);
    border-radius: 8px;
    border: 1px solid var(--border-color);
  }

  .comparison-item { flex: 1; text-align: center; }

  .comparison-label {
    font-size: 0.6875rem;
    color: var(--text-secondary);
    margin-bottom: 0.25rem;
    text-transform: uppercase;
    font-weight: 500;
  }

  .comparison-value {
    font-size: 1rem;
    font-weight: 600;
    padding: 0.25rem;
    border-radius: 6px;
  }

  .comparison-value.video { color: #ef4444; background: rgba(239, 68, 68, 0.1); }
  .comparison-value.audio { color: #10b981; background: rgba(16, 185, 129, 0.1); }
  .comparison-arrow { color: var(--text-secondary); }

  /* Radio choice group */
  .choice-group {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .choice-option {
    display: flex;
    align-items: flex-start;
    gap: 0.625rem;
    padding: 0.625rem 0.75rem;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s ease;
    background: var(--background-color);
  }

  .choice-option:hover {
    border-color: var(--primary-color, #3b82f6);
    background: rgba(59, 130, 246, 0.02);
  }

  .choice-option.selected {
    border-color: var(--primary-color, #3b82f6);
    background: rgba(59, 130, 246, 0.04);
  }

  :global([data-theme='dark']) .choice-option.selected {
    background: rgba(59, 130, 246, 0.08);
  }

  .choice-option input[type="radio"] {
    width: 18px;
    height: 18px;
    margin-top: 1px;
    cursor: pointer;
    accent-color: var(--primary-color, #3b82f6);
    flex-shrink: 0;
  }

  .choice-content {
    display: flex;
    flex-direction: column;
    gap: 0.1875rem;
    min-width: 0;
  }

  .choice-header {
    display: flex;
    align-items: center;
    gap: 0.375rem;
  }

  .choice-header svg {
    flex-shrink: 0;
    color: var(--text-secondary);
  }

  .choice-option.selected .choice-header svg {
    color: var(--primary-color, #3b82f6);
  }

  .choice-title {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .choice-badge {
    font-size: 0.6rem;
    font-weight: 600;
    padding: 0.0625rem 0.375rem;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .choice-badge.recommended {
    background: rgba(16, 185, 129, 0.12);
    color: #10b981;
    border: 1px solid rgba(16, 185, 129, 0.25);
  }

  .choice-desc {
    font-size: 0.6875rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .metadata-note {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    color: var(--text-tertiary, #94a3b8);
    font-size: 0.75rem;
  }

  @media (max-width: 480px) {
    .comparison { flex-direction: column; gap: 0.375rem; }
    .comparison-arrow svg { transform: rotate(90deg); }
  }
</style>
