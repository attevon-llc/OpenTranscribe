<script lang="ts">
  /**
   * Transcript export and copy for the file-detail page: the comments confirmation, the TXT
   * options dialog, the lazily loaded Word/PDF options dialog, and the server-side copy.
   *
   * Every byte comes from the SERVER (issue #673 downloads, #821 copy, #1278 documents), so
   * the admin `export_locked` floor and the reader's redaction policy decide what leaves the
   * app. Nothing here serializes a transcript, and a failed request is never answered with a
   * local fallback. The page calls `openExport` / `copyTranscript` via `bind:this` and reads
   * `documentBusy` / `copyStatus` back through `bind:`.
   */
  import { onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { loadTxtPrefs, saveTxtPrefs } from '$lib/export/txtExportPrefs';
  import { requestTranscriptExport } from '$lib/export/requestTranscriptExport';
  import { exportErrorKey } from '$lib/export/exportErrors';
  import { loadDocumentPrefs, saveDocumentPrefs } from '$lib/export/documentExportPrefs';
  import { documentTemplateLabels, startDocumentExport } from '$lib/export/requestDocumentExport';
  import { triggerAnchorDownload } from '$lib/export/anchorDownload';
  import type { ExportJobStream } from '$lib/export/exportJobStream';
  import { copyToClipboard } from '$lib/utils/clipboard';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import TxtExportOptionsModal from '$components/fileDetail/TxtExportOptionsModal.svelte';

  export let file: { uuid: string; filename: string } | null = null;
  export let diarizationDisabled = false;
  export let redactionActive = false;
  export let showOriginal = false;
  export let canViewOriginal = false;
  /** The page's cancellable `setTimeout` (`pageTimers.schedule`). */
  export let schedule: (callback: () => void, delayMs: number) => void;
  /** True from the Word/PDF confirm click until the server job settles (bound by the page). */
  export let documentBusy = false;
  /** Copy-button state for the transcript view modal (bound by the page). */
  export let copyStatus: 'idle' | 'copying' | 'copied' | 'failed' | 'empty' = 'idle';

  // Confirmation modal state
  let showExportConfirmation = false;
  let pendingExportFormat = '';

  // TXT export options modal state (localStorage persistence in $lib/export/txtExportPrefs)
  let showTxtExportOptions = false;
  let txtExportOptions = { includeTimestamps: true, includeSpeakers: true, includeComments: false, hasComments: false };

  // DOCX/PDF export (issue #1278). The options modal is a dynamic import: it is only ever
  // needed after someone picks Word or PDF, so it stays out of this route's initial chunk.
  let DocumentExportOptionsModal: typeof import('$components/fileDetail/DocumentExportOptionsModal.svelte').default | null = null;
  let showDocumentExportOptions = false;
  let documentFormat: 'docx' | 'pdf' = 'pdf';
  let documentOptions = { ...loadDocumentPrefs(), includeComments: false, hasComments: false };
  let documentStream: ExportJobStream | null = null;

  // Scroll lock for the TXT dialog's custom .modal-overlay
  let _prevTxtExport = false;
  $: {
    if (showTxtExportOptions !== _prevTxtExport) {
      if (showTxtExportOptions) lockScroll();
      else unlockScroll();
      _prevTxtExport = showTxtExportOptions;
    }
  }

  /**
   * Start an export in `format`. `hasComments` comes from CommentSection's reported count;
   * refetching comments here just to set a boolean put a round trip between clicking a
   * format and seeing the options dialog.
   */
  export async function openExport(format: string, hasComments: boolean) {
    if (format === 'docx' || format === 'pdf') {
      await openDocumentExportOptions(format, hasComments);
      return;
    }

    if (format === 'txt') {
      // Show TXT-specific options modal (timestamps, speakers, comments)
      const prefs = loadTxtPrefs();
      txtExportOptions = {
        ...prefs,
        includeComments: false,
        hasComments,
        // When diarization disabled, force speakers off
        ...(diarizationDisabled ? { includeSpeakers: false } : {})
      };
      showTxtExportOptions = true;
      return;
    }

    // For other formats: use the existing comments-only confirmation flow
    if (!hasComments) {
      pendingExportFormat = format;
      processExportWithComments(false);
      return;
    }

    // If comments exist, show confirmation modal
    pendingExportFormat = format;
    showExportConfirmation = true;
  }

  /** Resolved i18n strings the server renders into the export — it stays translation-free. */
  function exportLabels() {
    return {
      speaker_default_label: $t('fileDetail.speakerDefault'),
      user_comment_label: $t('fileDetail.userComment'),
      comment_type_label: $t('fileDetail.commentType'),
      csv_header_default: $t('fileDetail.csvHeaderDefault'),
      csv_header_with_comments: $t('fileDetail.csvHeaderWithComments'),
    };
  }

  // Issue #821: the transcript modal used to build the consolidated transcript in the
  // browser and copy that. A client-side serializer has nothing to ask about policy, so
  // the admin `export_locked` floor was bypassed on every copy — and worse when the reader
  // had "Show original" on, because the store then holds unredacted text. It also copied
  // only the segments paginated in so far. Same server path as every download format.
  function settleCopyStatus(next: 'copied' | 'failed' | 'empty') {
    copyStatus = next;
    // The page's `schedule`, not a bare setTimeout: the page's onDestroy cancels every
    // deferred callback through it, and a stray one firing after teardown is the exact
    // thing `cancels its pending timers on unmount` pins.
    schedule(() => (copyStatus = 'idle'), 2000);
  }

  export async function copyTranscript() {
    if (!file?.uuid || copyStatus === 'copying') return;
    copyStatus = 'copying';
    try {
      const { data } = await requestTranscriptExport<string>({
        fileUuid: file.uuid,
        format: 'txt',
        includeComments: false,
        includeTimestamps: true,
        includeSpeakers: !diarizationDisabled,
        showOriginal,
        labels: exportLabels(),
        responseType: 'text',
      });
      const text = typeof data === 'string' ? data : String(data ?? '');
      if (!text.trim()) {
        settleCopyStatus('empty');
        return;
      }
      await copyToClipboard(
        text,
        () => settleCopyStatus('copied'),
        () => settleCopyStatus('failed')
      );
    } catch (error: unknown) {
      // A refusal (503 policy unavailable, 409 redaction scan unfinished) stays a refusal;
      // falling back to a local copy here would reinstate the bypass this closes.
      console.error('Error copying transcript:', error);
      toastStore.error($t(exportErrorKey(error)));
      settleCopyStatus('failed');
    }
  }

  async function processExportWithComments(includeComments: boolean, txtOptions?: { includeTimestamps: boolean; includeSpeakers: boolean }) {
    const format = pendingExportFormat;
    if (!file) return;

    try {
      const filename = file.filename.replace(/\.[^/.]+$/, '');

      // Serialization happens server-side (issue #673) so the admin `export_locked`
      // floor is consulted for every format, not just subtitle downloads. The
      // resolved i18n strings still travel from here — the backend stays
      // translation-free, matching the rest of the API.
      const {
        data,
        contentType,
        filename: serverFilename,
      } = await requestTranscriptExport<BlobPart>({
        fileUuid: file.uuid,
        format,
        includeComments,
        includeTimestamps: txtOptions ? txtOptions.includeTimestamps : true,
        includeSpeakers: txtOptions ? txtOptions.includeSpeakers : true,
        // Mirrors every other read on this page (transcript/segments/subtitles): an owner
        // who toggled "Show original" expects the export to match what they are looking
        // at. The server still refuses it under the admin export_locked floor.
        showOriginal,
        labels: exportLabels(),
      });

      const blob = new Blob([data], { type: contentType });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      // The server names the file (RFC 6266 `filename*`, any script); the recomputed name
      // is only for a response that carried no Content-Disposition at all.
      link.download = serverFilename ?? `${filename}.${format}`;
      link.click();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      // A refusal (409 redaction scan unfinished, 503 policy unavailable) is told to the user,
      // never swallowed: a button that silently does nothing reads as a broken app.
      console.error('Error exporting transcript:', error);
      toastStore.error($t(exportErrorKey(error)));
    }
  }

  // Confirmation modal handlers
  function handleExportConfirm() {
    processExportWithComments(true);
  }

  function handleExportCancel() {
    processExportWithComments(false);
  }

  function handleExportModalClose() {
    // Just close the modal without doing anything
    showExportConfirmation = false;
  }

  // TXT export options modal handler
  async function handleTxtExportConfirm() {
    showTxtExportOptions = false;
    saveTxtPrefs({
      includeTimestamps: txtExportOptions.includeTimestamps,
      includeSpeakers: txtExportOptions.includeSpeakers
    });
    pendingExportFormat = 'txt';
    await processExportWithComments(txtExportOptions.includeComments, {
      includeTimestamps: txtExportOptions.includeTimestamps,
      includeSpeakers: txtExportOptions.includeSpeakers
    });
  }

  async function openDocumentExportOptions(format: 'docx' | 'pdf', hasComments: boolean) {
    documentFormat = format;
    documentOptions = {
      ...loadDocumentPrefs(),
      includeComments: false,
      hasComments,
      // When diarization is disabled, force speakers off
      ...(diarizationDisabled ? { includeSpeakers: false } : {}),
    };
    DocumentExportOptionsModal ??= (
      await import('$components/fileDetail/DocumentExportOptionsModal.svelte')
    ).default;
    showDocumentExportOptions = true;
  }

  // The document is built, masked and numbered on the SERVER (issue #1278); the browser only
  // queues the job, waits for its presigned URL and saves it. No local fallback on failure.
  async function handleDocumentExportConfirm() {
    showDocumentExportOptions = false;
    if (!file || documentBusy) return;
    saveDocumentPrefs({
      includeTimestamps: documentOptions.includeTimestamps,
      includeSpeakers: documentOptions.includeSpeakers,
      lineNumbering: documentOptions.lineNumbering,
      pageSize: documentOptions.pageSize,
    });
    documentBusy = true;
    try {
      documentStream?.close();
      documentStream = await startDocumentExport({
        fileUuid: file.uuid,
        format: documentFormat,
        includeComments: documentOptions.includeComments,
        includeTimestamps: documentOptions.includeTimestamps,
        includeSpeakers: documentOptions.includeSpeakers,
        lineNumbering: documentOptions.lineNumbering,
        pageSize: documentOptions.pageSize,
        showOriginal,
        labels: {
          speaker_default_label: $t('fileDetail.speakerDefault'),
          user_comment_label: $t('fileDetail.userComment'),
          redacted_label: $t('documentExport.redactedLabel'),
          ...documentTemplateLabels($t),
        },
      });
      const result = await documentStream.result;
      documentStream = null;
      triggerAnchorDownload(result.url, result.filename);
      toastStore.success($t('documentExport.ready'));
    } catch (error) {
      console.error('Error exporting document:', error);
      toastStore.error($t(exportErrorKey(error)));
    } finally {
      documentBusy = false;
    }
  }

  onDestroy(() => {
    documentStream?.close();
    // Release the scroll lock if the TXT dialog was open when the page went away
    if (_prevTxtExport) unlockScroll();
  });
</script>

<!-- Export Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showExportConfirmation}
  title={$t('exportConfirm.title')}
  message={$t('exportConfirm.message')}
  confirmText={$t('exportConfirm.includeComments')}
  cancelText={$t('exportConfirm.exportWithout')}
  on:confirm={handleExportConfirm}
  on:cancel={handleExportCancel}
  on:close={handleExportModalClose}
/>

<!-- TXT Export Options Modal -->
<TxtExportOptionsModal
  bind:show={showTxtExportOptions}
  bind:includeTimestamps={txtExportOptions.includeTimestamps}
  bind:includeSpeakers={txtExportOptions.includeSpeakers}
  bind:includeComments={txtExportOptions.includeComments}
  hasComments={txtExportOptions.hasComments}
  {diarizationDisabled}
  {redactionActive}
  {showOriginal}
  {canViewOriginal}
  on:confirm={handleTxtExportConfirm}
  on:close={() => showTxtExportOptions = false}
/>

<!-- DOCX / PDF Export Options Modal (loaded on first use) -->
{#if DocumentExportOptionsModal}
  <svelte:component
    this={DocumentExportOptionsModal}
    bind:show={showDocumentExportOptions}
    format={documentFormat}
    bind:includeTimestamps={documentOptions.includeTimestamps}
    bind:includeSpeakers={documentOptions.includeSpeakers}
    bind:includeComments={documentOptions.includeComments}
    bind:lineNumbering={documentOptions.lineNumbering}
    bind:pageSize={documentOptions.pageSize}
    hasComments={documentOptions.hasComments}
    {diarizationDisabled}
    {redactionActive}
    {showOriginal}
    {canViewOriginal}
    on:confirm={handleDocumentExportConfirm}
    on:close={() => (showDocumentExportOptions = false)}
  />
{/if}
