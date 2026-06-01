<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let file: any = null;
  export let diarizationDisabled: boolean = false;
  export let isEditingSpeakers: boolean = false;
  export let isDownloading: boolean = false;
  export let currentDownload: any = undefined;
  export let isVideoFile: boolean = false;
  export let canEmbedSubtitles: boolean = false;

  const dispatch = createEventDispatcher();

  // Export dropdown state (click-based for mobile touch support)
  let showExportDropdown = false;

  function toggleExportDropdown(event: MouseEvent) {
    event.stopPropagation();
    showExportDropdown = !showExportDropdown;
  }

  function closeExportDropdown() {
    showExportDropdown = false;
  }

  let showDownloadDropdown = false;

  function toggleDownloadDropdown(event: MouseEvent) {
    event.stopPropagation();
    showDownloadDropdown = !showDownloadDropdown;
  }

  function closeDownloadDropdown() {
    showDownloadDropdown = false;
  }

  function selectDownload(mode: string) {
    closeDownloadDropdown();
    dispatch('download', { mode });
  }

  function exportTranscript(format: string) {
    dispatch('exportTranscript', { format });
  }

  function toggleSpeakerEditor() {
    dispatch('toggleSpeakerEditor');
  }
</script>

<!-- Close dropdowns on outside click -->
<svelte:window on:click={() => { closeExportDropdown(); closeDownloadDropdown(); }} />

<div class="transcript-actions">
  <div class="export-dropdown" class:open={showExportDropdown}>
    <button
      class="export-transcript-button"
      title={$t('transcript.exportTitle')}
      on:click={toggleExportDropdown}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
      </svg>
      {$t('transcript.export')}
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect>
        <line x1="9" y1="9" x2="15" y2="9"></line>
        <line x1="9" y1="13" x2="15" y2="13"></line>
        <line x1="9" y1="17" x2="11" y2="17"></line>
      </svg>
    </button>
    {#if showExportDropdown}
      <!-- svelte-ignore a11y-click-events-have-key-events -->
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div class="export-dropdown-content" on:click|stopPropagation>
        <button
          on:click={() => { exportTranscript('txt'); closeExportDropdown(); }}
          title={$t('transcript.exportTextTitle')}
        >{$t('transcript.exportText')}</button>
        <button
          on:click={() => { exportTranscript('json'); closeExportDropdown(); }}
          title={$t('transcript.exportJsonTitle')}
        >{$t('transcript.exportJson')}</button>
        <button
          on:click={() => { exportTranscript('csv'); closeExportDropdown(); }}
          title={$t('transcript.exportCsvTitle')}
        >{$t('transcript.exportCsv')}</button>
        <button
          on:click={() => { exportTranscript('srt'); closeExportDropdown(); }}
          title={$t('transcript.exportSrtTitle')}
        >{$t('transcript.exportSrt')}</button>
        <button
          on:click={() => { exportTranscript('vtt'); closeExportDropdown(); }}
          title={$t('transcript.exportVttTitle')}
        >{$t('transcript.exportVtt')}</button>
      </div>
    {/if}
  </div>

  {#if !diarizationDisabled}
  <button
    class="edit-speakers-button"
    on:click={toggleSpeakerEditor}
    title={$t('transcript.editSpeakersTitle')}
  >
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 20h9"></path>
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path>
    </svg>
    {isEditingSpeakers ? $t('transcript.hideSpeakerEditor') : $t('transcript.editSpeakers')}
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
      <circle cx="12" cy="7" r="4"></circle>
    </svg>
  </button>
  {/if}

  {#if file && file.download_url}
    <div class="export-dropdown download-dropdown" class:open={showDownloadDropdown}>
      <button
        class="action-button download-button"
        class:downloading={isDownloading}
        class:processing={currentDownload?.status === 'processing'}
        disabled={isDownloading}
        on:click={toggleDownloadDropdown}
        title={isDownloading ? $t('transcript.processing') : $t('transcript.download')}
      >
        {#if isDownloading}
          <svg class="spinner" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 11-6.219-8.56"/>
          </svg>
          {currentDownload?.status === 'preparing' ? $t('transcript.preparing') : $t('transcript.processing')}
        {:else}
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
            <polyline points="7 10 12 15 17 10"></polyline>
            <line x1="12" y1="15" x2="12" y2="3"></line>
          </svg>
          {$t('transcript.download')}
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="6 9 12 15 18 9"></polyline>
          </svg>
        {/if}
      </button>
      {#if showDownloadDropdown && !isDownloading}
        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div class="export-dropdown-content" on:click|stopPropagation>
          {#if canEmbedSubtitles}
            <button on:click={() => selectDownload('video_subtitles')}>{$t('transcript.downloadVideoSubtitles')}</button>
          {/if}
          {#if isVideoFile}
            <button on:click={() => selectDownload('video_original')}>{$t('transcript.downloadOriginalVideo')}</button>
          {/if}
          <div class="download-dropdown-label">{$t('transcript.downloadAudio')}</div>
          <button on:click={() => selectDownload('audio_mp3')}>{$t('transcript.downloadAudioMp3')}</button>
          <button on:click={() => selectDownload('audio_wav')}>{$t('transcript.downloadAudioWav')}</button>
          <button on:click={() => selectDownload('audio_original')}>{$t('transcript.downloadAudioOriginal')}</button>
        </div>
      {/if}
    </div>
  {/if}

</div>

<style>
  .transcript-actions {
    display: flex;
    gap: 12px;
    margin-top: 16px;
    flex-wrap: wrap;
  }

  .export-dropdown {
    position: relative;
    display: inline-block;
  }

  .export-transcript-button,
  .edit-speakers-button,
  .action-button {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 16px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-primary);
    text-decoration: none;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .export-transcript-button:hover,
  .edit-speakers-button:hover,
  .action-button:hover {
    background: var(--surface-hover);
    border-color: var(--border-hover);
  }

  .export-dropdown-content {
    position: absolute;
    top: 100%;
    left: 0;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
    z-index: 100;
    min-width: 200px;
  }

  .export-dropdown-content button {
    display: block;
    width: 100%;
    padding: 10px 16px;
    background: none;
    border: none;
    text-align: left;
    color: var(--text-primary);
    font-size: 14px;
    cursor: pointer;
    transition: background-color 0.2s ease;
  }

  .export-dropdown-content button:hover {
    background: var(--surface-hover);
  }

  .export-dropdown-content button:first-child {
    border-radius: 6px 6px 0 0;
  }

  .export-dropdown-content button:last-child {
    border-radius: 0 0 6px 6px;
  }

  /* The Download dropdown is the rightmost action — align it to the right edge. */
  .download-dropdown .export-dropdown-content {
    left: auto;
    right: 0;
  }

  .download-dropdown-label {
    padding: 8px 16px 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    border-top: 1px solid var(--border-color);
  }

  /* Enhanced download button styles */
  .download-button:disabled {
    opacity: 0.7;
    cursor: not-allowed;
  }

  .download-button.downloading {
    background: #3b82f6;
    color: white;
    border-color: var(--primary-color);
  }

  .download-button.processing {
    background: var(--warning-color, #f59e0b);
    color: white;
    border-color: var(--warning-color, #f59e0b);
  }

  .spinner {
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 768px) {
    .transcript-actions {
      flex-direction: column;
    }
  }
</style>
