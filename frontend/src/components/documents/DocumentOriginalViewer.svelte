<script lang="ts">
  /**
   * Renders the ORIGINAL uploaded file. PDFs render natively in an <iframe> — no
   * library needed. DOCX/PPTX/XLSX have no in-browser renderer (confirmed: nothing
   * in package.json), so those fall back to a download-only message. HTML/text
   * formats render inline via the same presigned URL.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import { getDocumentDownloadUrl } from '$lib/api/documents';
  import { isNativelyRenderable } from '$lib/types/document';

  export let documentUuid: string;
  export let contentType: string;
  export let filename: string;

  let loading = true;
  let error = '';
  let inlineUrl: string | null = null;
  let downloadUrl: string | null = null;

  $: canRenderInline = isNativelyRenderable(contentType);
  $: isPdf = contentType === 'application/pdf';

  onMount(async () => {
    try {
      const [inline, download] = await Promise.all([
        canRenderInline ? getDocumentDownloadUrl(documentUuid, false) : Promise.resolve(null),
        getDocumentDownloadUrl(documentUuid, true),
      ]);
      inlineUrl = inline?.url ?? null;
      downloadUrl = download.url;
    } catch (err) {
      error = $t('documents.originalLoadFailed');
    } finally {
      loading = false;
    }
  });
</script>

<div class="original-viewer">
  {#if loading}
    <div class="viewer-state">
      <Spinner size="medium" />
    </div>
  {:else if error}
    <div class="viewer-state">
      <p>{error}</p>
    </div>
  {:else if canRenderInline && inlineUrl}
    {#if isPdf}
      <iframe class="pdf-frame" src={inlineUrl} title={filename}></iframe>
    {:else}
      <iframe class="text-frame" src={inlineUrl} title={filename} sandbox=""></iframe>
    {/if}
  {:else}
    <div class="no-preview">
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
        <polyline points="14 2 14 8 20 8"></polyline>
      </svg>
      <p class="no-preview-title">{$t('documents.noPreviewAvailable')}</p>
      <p class="no-preview-hint">{$t('documents.noPreviewHint')}</p>
      {#if downloadUrl}
        <a class="download-btn" href={downloadUrl} download={filename}>
          {$t('documents.download')}
        </a>
      {/if}
    </div>
  {/if}
</div>

<style>
  .original-viewer {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 480px;
  }

  .viewer-state {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 1;
    color: var(--text-secondary);
  }

  .pdf-frame,
  .text-frame {
    flex: 1;
    width: 100%;
    min-height: 480px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: white;
  }

  .no-preview {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    flex: 1;
    padding: 3rem 1rem;
    color: var(--text-secondary);
    text-align: center;
  }

  .no-preview-title {
    margin: 0;
    font-weight: 600;
    color: var(--text-primary);
  }

  .no-preview-hint {
    margin: 0;
    font-size: 0.85rem;
    max-width: 320px;
  }

  .download-btn {
    margin-top: 0.5rem;
    padding: 0.5rem 1.25rem;
    background: #3b82f6;
    color: white;
    border-radius: 8px;
    text-decoration: none;
    font-weight: 500;
    font-size: 0.875rem;
  }

  .download-btn:hover {
    background: #2563eb;
  }
</style>
