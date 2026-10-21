<!--
  LegalDocumentViewer.svelte — read-only modal for one versioned legal document.

  The text is served by the API (English only, vendored from the central legal repo). It is
  trusted, but still rendered through the shared markdown sanitizer.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { getLegalDocument, type LegalDocument } from '$lib/api/legal';
  import { renderLegalMarkdown } from '$lib/legal/renderLegalMarkdown';

  export let isOpen = false;
  export let docKey = '';
  export let onClose: () => void = () => {};

  let doc: LegalDocument | null = null;
  let loading = false;
  let failed = false;
  let loadedKey = '';

  $: loadIfNeeded(isOpen, docKey);

  function loadIfNeeded(open: boolean, key: string) {
    if (open && key && key !== loadedKey) void load(key);
  }

  async function load(key: string) {
    loadedKey = key;
    loading = true;
    failed = false;
    doc = null;
    try {
      doc = await getLegalDocument(key);
    } catch (err) {
      console.error('Error loading legal document:', err);
      failed = true;
      loadedKey = '';
    } finally {
      loading = false;
    }
  }

  let html = '';
  $: void renderHtml(doc);

  async function renderHtml(current: LegalDocument | null) {
    const rendered = current ? await renderLegalMarkdown(current.markdown) : '';
    // A newer document may have been picked while the sanitizer module loaded.
    if (current === doc) html = rendered;
  }
  $: fingerprint = doc ? doc.sha256.slice(0, 12) : '';
</script>

<BaseModal
  {isOpen}
  title={doc?.title ?? $t('legal.viewer.title')}
  size="lg"
  zIndex="calc(var(--z-modal) + 10)"
  {onClose}
>
  {#if loading}
    <div class="legal-state"><Spinner size="small" /> {$t('common.loading')}</div>
  {:else if failed}
    <p class="legal-state" role="alert">{$t('legal.viewer.loadFailed')}</p>
  {:else if doc}
    {#if doc.status === 'draft'}
      <div class="legal-draft-banner" data-testid="legal-draft-banner" role="note">
        {$t('legal.draftBadge')}
      </div>
    {/if}
    <p class="legal-meta">
      <span>{$t('legal.viewer.version', { version: doc.version })}</span>
      <span data-testid="legal-fingerprint" title={doc.sha256}>
        {$t('legal.viewer.fingerprint', { hash: fingerprint })}
      </span>
      <span>{$t('legal.viewer.officialText')}</span>
    </p>
    <!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized by renderLegalMarkdown -->
    <div class="legal-prose" data-testid="legal-prose" dir="ltr">{@html html}</div>
  {/if}

  <svelte:fragment slot="footer">
    <button class="btn btn-secondary" on:click={onClose}>{$t('common.close')}</button>
  </svelte:fragment>
</BaseModal>

<style>
  .legal-state {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-secondary);
  }

  .legal-draft-banner {
    margin-block-end: 12px;
    padding: 10px 14px;
    border: 1px solid var(--warning-color);
    border-inline-start-width: 4px;
    border-radius: 8px;
    background-color: rgba(var(--warning-color-rgb), 0.12);
    color: var(--text-primary);
    font-weight: var(--font-weight-semibold);
  }

  .legal-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 16px;
    margin: 0 0 12px;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .legal-prose {
    line-height: var(--line-height-body);
    text-align: start;
  }

  .legal-prose :global(h1),
  .legal-prose :global(h2),
  .legal-prose :global(h3) {
    margin-block: 1em 0.4em;
  }

  .legal-prose :global(blockquote) {
    margin-inline: 0;
    padding-inline-start: 12px;
    border-inline-start: 3px solid var(--border-color);
    color: var(--text-secondary);
  }

  .legal-prose :global(table) {
    border-collapse: collapse;
  }

  .legal-prose :global(th),
  .legal-prose :global(td) {
    padding: 4px 8px;
    border: 1px solid var(--border-color);
  }
</style>
