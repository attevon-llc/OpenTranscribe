<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import {
    AUDIO_ONLY_PREVIEW_FORMATS,
    BROWSER_PLAYABLE_FORMATS,
    CONVERTED_AUDIO_FORMATS,
    MEDIA_TYPE_BY_EXTENSION,
    fileExtension
  } from '$lib/utils/mediaFormats';

  export let file: File | null = null;

  const dispatch = createEventDispatcher<{
    fileSelect: { file: File };
    multipleFiles: { files: File[] };
    fileRemove: void;
    acknowledgeDuplicate: void;
    continueAnyway: void;
  }>();

  let fileInput: HTMLInputElement;
  let dropZoneEl: HTMLDivElement | null = null;
  let drag = false;
  let dragDropCleanup: (() => void) | null = null;

  function formatFileSize(bytes: number): string {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
  }

  /**
   * Normalize the MIME type where the browser gave us none, then hand the file up.
   *
   * This panel deliberately does NOT validate — the parent owns type/size rejection and
   * renders the error, so every path here dispatches `fileSelect`. It previously carried
   * type and size guard branches that each dispatched the identical event and returned,
   * plus a 15 GB constant none of them could enforce (issue #298). Validate in
   * `FileUploader`, against `$lib/utils/uploadLimits`.
   */
  function handleFileSelect(selectedFile: File) {
    let processedFile: File = selectedFile;

    if (!selectedFile.type) {
      const extension = fileExtension(selectedFile.name);
      const mimeType = MEDIA_TYPE_BY_EXTENSION[extension];
      if (extension && mimeType) {
        processedFile = new File([selectedFile], selectedFile.name, {
          type: mimeType,
          lastModified: selectedFile.lastModified
        });
      }
    }

    dispatch('fileSelect', { file: processedFile });
  }

  function handleFileInputChange(e: Event) {
    const target = e.target as HTMLInputElement;
    const files = target.files;
    if (!files || files.length === 0) return;

    if (files.length === 1) {
      handleFileSelect(files[0]);
    } else {
      dispatch('multipleFiles', { files: Array.from(files) });
    }
    target.value = '';
  }

  function openFileDialog() {
    fileInput?.click();
  }

  // Drag-and-drop
  function handleDragOver(e: DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    drag = true;
  }

  function handleDragLeave(e: DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    drag = false;
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    drag = false;

    const dt = e.dataTransfer;
    if (!dt) return;

    const files = dt.files;
    if (files && files.length > 0) {
      if (files.length === 1) {
        handleFileSelect(files[0]);
      } else {
        dispatch('multipleFiles', { files: Array.from(files) });
      }
    }
  }

  function initDragAndDrop(dropZone: HTMLDivElement) {
    dropZone.addEventListener('dragover', handleDragOver);
    dropZone.addEventListener('dragleave', handleDragLeave);
    dropZone.addEventListener('drop', handleDrop);

    return () => {
      dropZone.removeEventListener('dragover', handleDragOver);
      dropZone.removeEventListener('dragleave', handleDragLeave);
      dropZone.removeEventListener('drop', handleDrop);
    };
  }

  // Rebind whenever the drop-zone element (re)appears. `{#if !file}` destroys
  // and recreates it (select a file, then clear it), and the previous
  // `onMount`-only `getElementById` bind only ever attached to the FIRST
  // instance — the recreated node had no drag/drop listeners at all, so a
  // drop after clearing a file had nothing to call `preventDefault()`, and
  // the browser navigated away to the dropped file instead (#649).
  $: if (dropZoneEl) {
    if (dragDropCleanup) dragDropCleanup();
    dragDropCleanup = initDragAndDrop(dropZoneEl);
  } else if (dragDropCleanup) {
    dragDropCleanup();
    dragDropCleanup = null;
  }

  onDestroy(() => {
    if (dragDropCleanup) dragDropCleanup();
  });
</script>

<div class="file-panel">
  {#if !file}
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <div
      id="drop-zone"
      bind:this={dropZoneEl}
      class="drop-zone"
      class:active={drag}
      on:click={openFileDialog}
      on:keydown={(e) => e.key === 'Enter' && openFileDialog()}
      role="button"
      tabindex="0"
      title={$t('uploader.dropZoneTooltip')}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="17 8 12 3 7 8"></polyline>
        <line x1="12" y1="3" x2="12" y2="15"></line>
      </svg>
      <div class="upload-text">
        <span>{$t('uploader.dragDropFiles')}</span>
        <span class="or-text">{$t('uploader.orClickToBrowse')}</span>
        <span class="multi-file-hint">{$t('uploader.multipleFilesSupported')}</span>
      </div>
      <input
        type="file"
        accept="audio/*,video/*"
        multiple
        bind:this={fileInput}
        on:change={handleFileInputChange}
        style="display: none;"
      />
    </div>

    <div class="supported-formats">
      <p class="formats-heading">{$t('uploader.formatsHeading')}</p>
      <p data-testid="formats-playable">
        <span class="formats-label">{$t('uploader.formatsPlayable')}</span>
        {BROWSER_PLAYABLE_FORMATS.join(', ')}
      </p>
      <p data-testid="formats-converted">
        <span class="formats-label">{$t('uploader.formatsConverted')}</span>
        {CONVERTED_AUDIO_FORMATS.join(', ')}
      </p>
      <p data-testid="formats-audio-only">
        <span class="formats-label">{$t('uploader.formatsAudioOnly')}</span>
        {AUDIO_ONLY_PREVIEW_FORMATS.join(', ')}
      </p>
      <p class="formats-help">{$t('uploader.formatsHelp')}</p>
    </div>
  {:else}
    <div class="selected-file">
      <div class="file-info">
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
          <polyline points="2 17 12 22 22 17"></polyline>
          <polyline points="2 12 12 17 22 12"></polyline>
        </svg>
        <div>
          <p class="file-name">{file.name}</p>
          <p class="file-size">{formatFileSize(file.size)}</p>
        </div>
      </div>
      <button type="button" class="file-remove" on:click={() => dispatch('fileRemove')} title={$t('uploader.removeItem')}>
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>
  {/if}
</div>

<style>
  .file-panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .drop-zone {
    padding: 2.5rem 2rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
    border: 2px dashed var(--border-color);
    border-radius: 12px;
    background-color: var(--surface-color);
    cursor: pointer;
    transition: all 0.2s ease;
    text-align: center;
  }

  .drop-zone:hover,
  .drop-zone.active {
    border-color: var(--primary-color);
    background-color: rgba(59, 130, 246, 0.05);
  }

  :global([data-theme='dark']) .drop-zone:hover,
  :global([data-theme='dark']) .drop-zone.active {
    background-color: rgba(59, 130, 246, 0.1);
  }

  .drop-zone svg {
    width: 2.5rem;
    height: 2.5rem;
    color: var(--primary-on-surface);
    margin-bottom: 0.25rem;
  }

  .upload-text {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
    color: var(--text-color);
    font-size: 0.9375rem;
    line-height: 1.5;
  }

  .or-text {
    color: var(--text-light);
    font-size: 0.85em;
  }

  .multi-file-hint {
    color: var(--primary-on-surface);
    font-size: 0.8em;
    font-weight: 500;
    margin-top: 2px;
  }

  .supported-formats {
    text-align: center;
  }

  .supported-formats p {
    margin: 0;
    font-size: 0.8125rem;
    line-height: 1.5;
    color: var(--text-secondary);
  }

  .supported-formats .formats-heading {
    font-weight: 600;
    margin-bottom: 0.125rem;
  }

  .supported-formats .formats-label {
    font-weight: 500;
    color: var(--text-color);
  }

  .supported-formats .formats-help {
    margin-top: 0.25rem;
    font-size: 0.75rem;
  }

  .selected-file {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.75rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
  }

  .file-info {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    overflow: hidden;
  }

  .file-info svg {
    flex-shrink: 0;
    color: var(--primary-on-surface);
  }

  .file-name {
    font-weight: 500;
    font-size: 0.875rem;
    margin: 0;
    word-break: break-all;
  }

  .file-size {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin: 0.125rem 0 0 0;
  }

  .file-remove {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    /* Resets the global `button { padding: 0.6rem 1.2rem }` (38.4px), which is
       wider than this 28px box and would clamp the content box to zero, hiding
       the icon entirely (#746). */
    padding: 0;
    border: none;
    background: transparent;
    border-radius: 6px;
    cursor: pointer;
    color: var(--text-secondary);
    transition: all 0.15s ease;
  }

  .file-remove:hover {
    background: var(--button-hover, #f1f5f9);
    color: #ef4444;
  }
</style>
