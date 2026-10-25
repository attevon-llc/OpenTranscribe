<!--
  LegalDocumentViewer.svelte — read-only reader for one versioned legal document.

  The text is served by the API (English only, vendored from the central legal repo). It is
  trusted, but still rendered through the shared markdown sanitizer, and typeset by LegalProse.

  Opens as a dialog with a "Full screen" toggle in its header (BaseModal `fullscreen`), or
  straight into full screen with `startFullscreen`. Below 768px BaseModal is already full screen,
  so the toggle is hidden there. Escape and the X close the reader; focus returns to the opener.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import LegalProse from './LegalProse.svelte';
  import { getLegalDocument, type LegalDocument } from '$lib/api/legal';
  import { renderLegalMarkdown } from '$lib/legal/renderLegalMarkdown';

  export let isOpen = false;
  export let docKey = '';
  export let onClose: () => void = () => {};
  /** Open in full screen rather than as a dialog (reset each time the reader opens). */
  export let startFullscreen = false;

  let doc: LegalDocument | null = null;
  let loading = false;
  let failed = false;
  let loadedKey = '';
  let fullscreen = startFullscreen;

  let wasOpen = false;
  $: syncFullscreen(isOpen);

  function syncFullscreen(open: boolean) {
    if (open && !wasOpen) fullscreen = startFullscreen;
    wasOpen = open;
  }

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
  {fullscreen}
  title={doc?.title ?? $t('legal.viewer.title')}
  size="lg"
  zIndex="calc(var(--z-modal) + 10)"
  {onClose}
>
  <svelte:fragment slot="header-actions">
    <button
      type="button"
      class="btn btn-secondary btn-small legal-fullscreen-toggle"
      data-testid="legal-fullscreen-toggle"
      on:click={() => (fullscreen = !fullscreen)}
    >
      <MenuIcon name={fullscreen ? 'minimize' : 'maximize'} />
      {fullscreen ? $t('legal.viewer.exitFullscreen') : $t('legal.viewer.enterFullscreen')}
    </button>
  </svelte:fragment>

  <div class="legal-document" class:fullscreen>
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
      <LegalProse {html} />
    {/if}
  </div>

  <svelte:fragment slot="footer">
    <button class="btn btn-secondary" on:click={onClose}>{$t('common.close')}</button>
  </svelte:fragment>
</BaseModal>

<style>
  .legal-document.fullscreen {
    max-inline-size: var(--measure-prose);
    margin-inline: auto;
    padding-block: 0.5rem 2rem;
  }

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
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
  }

  .legal-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 16px;
    margin: 0 0 1.25rem;
    padding-block-end: 0.75rem;
    border-block-end: 1px solid var(--border-color);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  /* BaseModal is already full screen below 768px, so the toggle would do nothing there. */
  @media (max-width: 768px) {
    .legal-fullscreen-toggle {
      display: none;
    }
  }
</style>
