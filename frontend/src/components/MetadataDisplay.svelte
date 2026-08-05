<script lang="ts">
  import type { MediaFileDetail } from '$lib/types/media';
  import { slide } from 'svelte/transition';
  import { formatDuration } from '$lib/utils/formatting';
  import { authStore } from '$stores/auth';
  import { t } from '$stores/locale';

  export let file: MediaFileDetail | null = null;
  export let showMetadata: boolean = false;


  function toggleMetadata() {
    showMetadata = !showMetadata;
  }

</script>

<div class="metadata-dropdown-section">
  <button class="section-header" on:click={toggleMetadata} on:keydown={e => e.key === 'Enter' && toggleMetadata()} aria-expanded={showMetadata}>
    <h4 class="section-heading">{$t('metadata.fileDetails')}</h4>
    <span class="dropdown-toggle" aria-hidden="true">
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="transform: rotate({showMetadata ? '180deg' : '0deg'})">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </span>
  </button>

  {#if showMetadata}
  <div class="section-content" transition:slide={{ duration: 200 }}>
    <div class="metadata-panel">
      <div class="metadata-grid four-columns">
        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.uploadedBy')}:</span>
          <span class="metadata-value">{$authStore?.user?.email || $t('metadata.unknownUser')}</span>
        </div>

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.uploadDate')}:</span>
          <span class="metadata-value">{file && 'upload_time' in file && file.upload_time ? new Date(file.upload_time).toLocaleDateString() : $t('metadata.unknown')}</span>
        </div>

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.mediaType')}:</span>
          <span class="metadata-value">{file && 'content_type' in file ? file.content_type : $t('metadata.unknown')}</span>
        </div>

        {#if file && 'source_url' in file && file.source_url}
          <div class="metadata-item">
            <span class="metadata-label">{$t('metadata.source')}:</span>
            <span class="metadata-value">
              <a href={file.source_url} target="_blank" rel="noopener noreferrer" class="source-link">
                {file.source_url.length > 50 ? file.source_url.substring(0, 47) + '...' : file.source_url}
              </a>
            </span>
          </div>
        {/if}

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.duration')}:</span>
          <span class="metadata-value">
            {#if file && 'duration' in file && file.duration}
              {file.formatted_duration || $t('metadata.na')}
            {:else}
              {$t('metadata.unknown')}
            {/if}
          </span>
        </div>

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.fileSize')}:</span>
          <span class="metadata-value">
            {#if file && 'file_size' in file && file.file_size}
              {file.formatted_file_size || $t('metadata.na')}
            {:else}
              {$t('metadata.unknown')}
            {/if}
          </span>
        </div>

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.transcriptStatus')}:</span>
          <span class="metadata-value">
            {#if file?.status === 'completed'}
              <span class="status-completed">{$t('metadata.completed')} <i class="fas fa-check-circle"></i></span>
            {:else if file?.status === 'processing'}
              <span class="status-processing">{$t('metadata.processing')} <i class="fas fa-spinner fa-spin"></i></span>
            {:else if file?.status === 'error'}
              <span class="status-error">{$t('metadata.failed')} <i class="fas fa-exclamation-triangle"></i></span>
            {:else if file?.status === 'pending'}
              <span class="status-pending">{$t('metadata.pending')}</span>
            {:else}
              <span class="status-pending">{file?.display_status || file?.status || $t('metadata.unknown')}</span>
            {/if}
          </span>
        </div>

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.aiSummaryStatus')}:</span>
          <span class="metadata-value">
            {#if file?.summary_status === 'completed' || file?.summary || file?.summary_opensearch_id}
              <span class="status-completed">{$t('metadata.available')} <i class="fas fa-check-circle"></i></span>
            {:else if file?.summary_status === 'processing'}
              <span class="status-processing">{$t('metadata.processing')} <i class="fas fa-spinner fa-spin"></i></span>
            {:else if file?.summary_status === 'failed'}
              <span class="status-error">{$t('metadata.failed')} <i class="fas fa-exclamation-triangle"></i></span>
            {:else}
              <span class="status-pending">{$t('metadata.pending')}</span>
            {/if}
          </span>
        </div>

        {#if file && 'language' in file && file.language}
          <div class="metadata-item">
            <span class="metadata-label">{$t('metadata.language')}:</span>
            <span class="metadata-value">{file.language}</span>
          </div>
        {/if}

        {#if file && file.asr_provider}
          <div class="metadata-item">
            <span class="metadata-label">{$t('metadata.asrEngine')}:</span>
            <span class="metadata-value">{file.asr_provider}</span>
          </div>
        {/if}

        {#if file && file.asr_model}
          <div class="metadata-item">
            <span class="metadata-label">{$t('metadata.asrModel')}:</span>
            <span class="metadata-value">{file.asr_model}</span>
          </div>
        {/if}

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.createdDate')}:</span>
          <span class="metadata-value">{file && 'creation_date' in file && file.creation_date ? new Date(file.creation_date).toLocaleDateString() : $t('metadata.unknown')}</span>
        </div>

        <!-- Tech Specs -->
        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.resolution')}:</span>
          <span class="metadata-value">
            {#if file && 'resolution_width' in file && 'resolution_height' in file && file.resolution_width && file.resolution_height}
              {file.resolution_width}×{file.resolution_height}
            {:else}
              {$t('metadata.na')}
            {/if}
          </span>
        </div>

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.frameRate')}:</span>
          <span class="metadata-value">
            {#if file && 'frame_rate' in file && file.frame_rate}
              {typeof file.frame_rate === 'number' ? file.frame_rate.toFixed(0) : file.frame_rate}fps
            {:else}
              {$t('metadata.na')}
            {/if}
          </span>
        </div>

        <!-- Audio Info -->
        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.audioChannels')}:</span>
          <span class="metadata-value">
            {#if file && 'audio_channels' in file && file.audio_channels}
              {file.audio_channels}ch
            {:else}
              {$t('metadata.na')}
            {/if}
          </span>
        </div>

        <div class="metadata-item">
          <span class="metadata-label">{$t('metadata.sampleRate')}:</span>
          <span class="metadata-value">
            {#if file && 'audio_sample_rate' in file && file.audio_sample_rate}
              {Math.round(file.audio_sample_rate/1000)}kHz
            {:else}
              {$t('metadata.na')}
            {/if}
          </span>
        </div>
      </div>

      {#if file?.whisper_model || file?.diarization_model || file?.diarization_disabled || file?.embedding_mode}
        <div class="processing-models-section">
          <hr class="models-divider" />
          <div class="models-row">
            {#if file.whisper_model}
              <span class="model-badge">
                <span class="model-label">Whisper:</span>
                <span class="model-name">{file.whisper_model}</span>
              </span>
            {/if}
            {#if file.diarization_model}
              <span class="model-badge">
                <span class="model-label">{$t('metadata.diarizationLabel')}</span>
                <span class="model-name">{file.diarization_model}</span>
              </span>
            {:else if file.diarization_disabled}
              <span class="model-badge diarization-disabled">
                <span class="model-label">{$t('metadata.diarizationLabel')}</span>
                <span class="model-name">{$t('metadata.diarizationDisabled')}</span>
              </span>
            {/if}
            {#if file.embedding_mode}
              <span class="model-badge">
                <span class="model-label">{$t('metadata.embeddingsLabel')}</span>
                <span class="model-name">{file.embedding_mode}</span>
              </span>
            {/if}
          </div>
        </div>
      {/if}
    </div>
  </div>
  {/if}
</div>

<style>
  .metadata-dropdown-section {
    margin-bottom: 20px;
  }

  .section-header {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 12px 16px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .section-header:hover {
    background: var(--surface-hover);
    border-color: var(--border-hover);
  }

  .section-heading {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .dropdown-toggle svg {
    transition: transform 0.2s ease;
  }

  .section-content {
    border: 1px solid var(--border-color);
    border-top: none;
    border-radius: 0 0 8px 8px;
    background: var(--surface-color);
  }

  .metadata-panel {
    padding: 20px;
  }

  .metadata-grid {
    display: grid;
    gap: 16px;
    margin-bottom: 20px;
  }

  .metadata-grid.four-columns {
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  }

  .metadata-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .metadata-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .metadata-value {
    font-size: 14px;
    color: var(--text-primary);
    font-weight: 500;
  }

  .status-completed {
    color: var(--success-color);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .status-processing {
    color: var(--warning-color);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .status-error {
    color: var(--error-color);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .status-pending {
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .source-link {
    color: var(--primary-color);
    text-decoration: none;
    word-break: break-all;
    transition: color 0.2s ease;
  }

  .source-link:hover {
    color: var(--primary-hover);
    text-decoration: underline;
  }

  .processing-models-section {
    margin-top: 4px;
  }

  .models-divider {
    border: none;
    border-top: 1px solid var(--border-color);
    margin: 8px 0 12px 0;
  }

  .models-row {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    justify-content: center;
  }

  .model-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 10px;
    background: var(--surface-hover, rgba(0, 0, 0, 0.03));
    border: 1px solid var(--border-color);
    border-radius: 12px;
    font-size: 12px;
  }

  .model-badge .model-label {
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.3px;
    font-size: 10px;
  }

  .model-badge .model-name {
    font-family: 'SF Mono', 'Fira Code', 'Fira Mono', 'Roboto Mono', monospace;
    color: var(--text-primary);
    font-weight: 500;
  }

  .model-badge.diarization-disabled .model-name {
    color: var(--text-secondary);
    font-style: italic;
  }

  @media (max-width: 768px) {
    .metadata-grid.four-columns {
      grid-template-columns: 1fr;
    }
  }
</style>
