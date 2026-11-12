<script lang="ts">
  import ExportOptionsModal from './ExportOptionsModal.svelte';
  import { t } from '$stores/locale';
  import type { LineNumbering, PageSize } from '$lib/export/documentExportPrefs';

  // Visibility (two-way bound by the page)
  export let show = false;
  /** Which document is being prepared: the note under the options differs. */
  export let format: 'docx' | 'pdf' = 'pdf';

  // Option values (two-way bound to the page's options object)
  export let includeTimestamps = true;
  export let includeSpeakers = true;
  export let includeComments = false;
  export let hasComments = false;
  export let lineNumbering: LineNumbering = 'per_page';
  export let pageSize: PageSize = 'a4';

  // Read-only display context
  export let diarizationDisabled = false;
  export let redactionActive = false;
  export let showOriginal = false;
  export let canViewOriginal = false;
</script>

<ExportOptionsModal
  title={$t('documentExport.title')}
  description={$t('documentExport.description')}
  bind:show
  bind:includeTimestamps
  bind:includeSpeakers
  bind:includeComments
  {hasComments}
  {diarizationDisabled}
  {redactionActive}
  {showOriginal}
  {canViewOriginal}
  on:close
  on:confirm
>
  <svelte:fragment slot="fields">
    <div class="export-field">
      <label for="document-export-line-numbering">{$t('documentExport.lineNumbering')}</label>
      <select id="document-export-line-numbering" bind:value={lineNumbering}>
        <option value="per_page">{$t('documentExport.lineNumberingPerPage')}</option>
        <option value="continuous">{$t('documentExport.lineNumberingContinuous')}</option>
        <option value="none">{$t('documentExport.lineNumberingNone')}</option>
      </select>
    </div>
    <div class="export-field">
      <label for="document-export-page-size">{$t('documentExport.pageSize')}</label>
      <select id="document-export-page-size" bind:value={pageSize}>
        <option value="a4">{$t('documentExport.pageSizeA4')}</option>
        <option value="letter">{$t('documentExport.pageSizeLetter')}</option>
      </select>
    </div>
  </svelte:fragment>

  <p slot="note" class="export-format-note">
    {format === 'docx' ? $t('documentExport.docxNote') : $t('documentExport.pdfNote')}
  </p>
</ExportOptionsModal>

<style>
  .export-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
    font-size: 0.95rem;
    color: var(--text-color);
  }

  .export-format-note {
    margin: var(--space-4) 0 0;
    font-size: var(--font-size-md);
    color: var(--text-secondary);
    line-height: 1.5;
  }
</style>
