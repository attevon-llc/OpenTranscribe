<script lang="ts">
  /**
   * Document upload: pick file(s) → submit. Deliberately not the media wizard
   * (FileUploader.svelte) — no speaker/model/collection steps apply to a document.
   */
  import { onDestroy, onMount, createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import {
    documentUploadService,
    type DocumentUploadItem,
    type DocumentUploadEvent,
  } from '$lib/services/documentUploadService';

  const dispatch = createEventDispatcher<{ uploaded: { documentUuid: string } }>();

  const MAX_UPLOAD_BYTES = 256 * 1024 * 1024;

  let uploads: DocumentUploadItem[] = [];
  let isDragging = false;
  let fileInput: HTMLInputElement;
  let cleanup: (() => void) | null = null;

  onMount(() => {
    uploads = documentUploadService.getAllUploads();
    cleanup = documentUploadService.addEventListener((event: DocumentUploadEvent) => {
      uploads = documentUploadService.getAllUploads();
      if (event.type === 'completed') {
        const data = event.data as { uuid: string } | undefined;
        if (data?.uuid) dispatch('uploaded', { documentUuid: data.uuid });
      }
    });
  });

  onDestroy(() => cleanup?.());

  function formatFileSize(bytes: number): string {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  }

  function queueFiles(files: FileList | File[]) {
    const valid: File[] = [];
    const rejected: string[] = [];
    Array.from(files).forEach((f) => {
      if (f.size > MAX_UPLOAD_BYTES) {
        rejected.push(f.name);
        return;
      }
      valid.push(f);
    });
    if (rejected.length > 0) {
      toastStore.error(
        $t('documents.filesTooLarge', {
          count: rejected.length,
          maxSize: formatFileSize(MAX_UPLOAD_BYTES),
        })
      );
    }
    if (valid.length > 0) {
      documentUploadService.addFiles(valid);
    }
  }

  function handleFileInput(event: Event) {
    const target = event.target as HTMLInputElement;
    if (target.files && target.files.length > 0) {
      queueFiles(target.files);
      target.value = '';
    }
  }

  function handleDrop(event: DragEvent) {
    event.preventDefault();
    isDragging = false;
    if (event.dataTransfer?.files && event.dataTransfer.files.length > 0) {
      queueFiles(event.dataTransfer.files);
    }
  }

  function handleDragOver(event: DragEvent) {
    event.preventDefault();
    isDragging = true;
  }

  function handleDragLeave() {
    isDragging = false;
  }

  function removeUpload(id: string) {
    documentUploadService.removeUpload(id);
    uploads = documentUploadService.getAllUploads();
  }

  $: activeOrFailed = uploads.filter((u) => u.status !== 'completed');
</script>

<div class="upload-panel">
  <div
    class="dropzone"
    class:dragging={isDragging}
    on:drop={handleDrop}
    on:dragover={handleDragOver}
    on:dragleave={handleDragLeave}
    on:click={() => fileInput.click()}
    on:keydown={(e) => e.key === 'Enter' && fileInput.click()}
    role="button"
    tabindex="0"
  >
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
      <polyline points="14 2 14 8 20 8"></polyline>
      <line x1="12" y1="18" x2="12" y2="12"></line>
      <polyline points="9 15 12 12 15 15"></polyline>
    </svg>
    <p class="dropzone-title">{$t('documents.dropzoneTitle')}</p>
    <p class="dropzone-hint">{$t('documents.dropzoneHint')}</p>
    <input
      bind:this={fileInput}
      type="file"
      multiple
      accept=".pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.html,.htm,.md,.csv,.txt,.rtf"
      on:change={handleFileInput}
      hidden
    />
  </div>

  {#if activeOrFailed.length > 0}
    <div class="upload-list">
      {#each activeOrFailed as upload (upload.id)}
        <div class="upload-row">
          <div class="upload-row-main">
            <span class="upload-name" title={upload.name}>{upload.name}</span>
            <span class="upload-size">{formatFileSize(upload.size)}</span>
          </div>
          {#if upload.status === 'uploading' || upload.status === 'queued'}
            <div class="upload-progress-bar">
              <div class="upload-progress-fill" style="width: {upload.progress}%"></div>
            </div>
          {:else if upload.status === 'failed'}
            <div class="upload-error">
              <span>{upload.error}</span>
              <button type="button" class="upload-dismiss" on:click={() => removeUpload(upload.id)}>
                {$t('documents.dismiss')}
              </button>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .upload-panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .dropzone {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.375rem;
    padding: 2rem 1rem;
    border: 2px dashed var(--border-color);
    border-radius: 10px;
    cursor: pointer;
    color: var(--text-secondary);
    transition:
      border-color 0.15s ease,
      background-color 0.15s ease;
  }

  .dropzone:hover,
  .dropzone.dragging {
    border-color: var(--primary-color, #3b82f6);
    background: rgba(59, 130, 246, 0.05);
    color: var(--primary-color, #3b82f6);
  }

  :global(.dark) .dropzone:hover,
  :global(.dark) .dropzone.dragging {
    background: rgba(59, 130, 246, 0.1);
  }

  .dropzone-title {
    margin: 0;
    font-weight: 600;
    font-size: 0.9rem;
    color: var(--text-primary);
  }

  .dropzone-hint {
    margin: 0;
    font-size: 0.78rem;
  }

  .upload-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .upload-row {
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .upload-row-main {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    font-size: 0.8125rem;
    margin-bottom: 0.375rem;
  }

  .upload-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-primary);
  }

  .upload-size {
    flex-shrink: 0;
    color: var(--text-secondary);
  }

  .upload-progress-bar {
    height: 5px;
    background: var(--border-color);
    border-radius: 3px;
    overflow: hidden;
  }

  .upload-progress-fill {
    height: 100%;
    background: #3b82f6;
    transition: width 0.2s ease;
  }

  .upload-error {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.78rem;
    color: #ef4444;
  }

  .upload-dismiss {
    background: none;
    border: none;
    color: var(--primary-color, #3b82f6);
    cursor: pointer;
    font-size: 0.78rem;
    padding: 0;
    white-space: nowrap;
  }
</style>
