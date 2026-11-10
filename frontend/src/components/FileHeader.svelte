<script lang="ts">
  import type { MediaFileDetail } from '$lib/types/media';
  import { createEventDispatcher } from 'svelte';
  import axiosInstance from '$lib/axios';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import DemoAttribution from '$components/demo/DemoAttribution.svelte';
  import HeldFileBanner from '$components/legal-hold/HeldFileBanner.svelte';
  import { fileFrozen } from '$lib/legalHold/fileHoldContext';
  import { reportHoldRefusal } from '$lib/legalHold/refusal';
  import { demoMode } from '$stores/demo';
  import { t } from '$stores/locale';
  import { resolveMediaErrorI18nKey } from '$lib/i18n/mediaErrors';

  const dispatch = createEventDispatcher();
  // A held file's title is frozen (legal-hold plan, Lane D); the banner below says why.
  const frozen = fileFrozen();

  export let file: MediaFileDetail | null = null;
  export let currentProcessingStep: string = '';
  export let sharedPermission: string | null = null;

  // GH #960: the backend's fixed `error_reason` vocabulary is translated client-side;
  // `file.user_message` (server-authored English) is only the fallback for a reason this
  // client does not recognize, matching the pre-#960 behavior for that case.
  $: mediaErrorI18nKey = resolveMediaErrorI18nKey(file?.error_reason);

  let isExpanded = false;
  let isEditingTitle = false;
  let editedTitle = '';
  let isSaving = false;

  // Error notices share one dialog; there is nothing to run on confirm.
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;

  function notify(title: string, message: string) {
    confirmDialog.request(title, message, () => {}, { confirmText: $t('common.confirm') });
  }

  // Helper function to determine if text should be truncated
  function shouldTruncate(text: string): boolean {
    if (!text) return false;

    // Lowered threshold to ~80 characters to test (roughly 1.5 lines)
    return text.length > 80;
  }

  function toggleExpanded() {
    isExpanded = !isExpanded;
  }

  function getDisplayName(file: MediaFileDetail | null): string {
    return file?.title || file?.filename || $t('fileDetail.unknownFile');
  }

  function startEditingTitle() {
    if (!file) return;
    isEditingTitle = true;
    editedTitle = file.title || file.filename || '';
  }

  function cancelEditingTitle() {
    isEditingTitle = false;
    editedTitle = '';
  }

  async function saveTitle() {
    if (!file || isSaving) return;

    const trimmedTitle = editedTitle.trim();
    if (trimmedTitle.length === 0) {
      cancelEditingTitle();
      return;
    }

    if (trimmedTitle.length > 255) {
      notify($t('fileDetail.displayNameTooLong'), $t('fileDetail.displayNameTooLongMessage'));
      return;
    }

    isSaving = true;

    try {
      const response = await axiosInstance.put(`/files/${file.uuid}`, {
        title: trimmedTitle
      });

      if (response.data) {
        // Update the file object with the new title, and tell the page so its own `file`
        // is invalidated. Mutating this prop alone updates the header but leaves every
        // other consumer of `file` on the old title until a reload — the same trap #338
        // removed elsewhere.
        file.title = response.data.title;
        dispatch('titleUpdated', { title: response.data.title });
        isEditingTitle = false;
        editedTitle = '';
      } else {
        throw new Error('Failed to update display name');
      }
    } catch (error) {
      console.error('Error updating display name:', error);
      // A hold placed since the page loaded: say so (translated) instead of a generic failure.
      if (reportHoldRefusal(error)) cancelEditingTitle();
      else notify($t('fileDetail.saveFailed'), $t('fileDetail.saveFailedMessage'));
    } finally {
      isSaving = false;
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      void saveTitle();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      cancelEditingTitle();
    }
  }

</script>

<div class="file-header-main">
  <div class="file-title">
    {#if isEditingTitle}
      <div class="title-edit-container">
        <input
          type="text"
          bind:value={editedTitle}
          on:keydown={handleKeydown}
          class="title-input"
          placeholder={$t('fileDetail.enterDisplayName')}
          maxlength="255"
          disabled={isSaving}
        />
        <div class="edit-buttons">
          <button
            type="button"
            on:click={saveTitle}
            disabled={isSaving}
            class="btn btn-success save-btn"
            aria-label={$t('fileDetail.saveTitle')}
            title={$t('fileDetail.saveTitle')}
          >
            {#if isSaving}
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="spinning">
                <circle cx="12" cy="12" r="10"></circle>
                <polyline points="12 6 12 12 16 14"></polyline>
              </svg>
            {:else}
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="20 6 9 17 4 12"></polyline>
              </svg>
            {/if}
          </button>
          <button
            type="button"
            on:click={cancelEditingTitle}
            disabled={isSaving}
            class="btn btn-secondary cancel-btn"
            aria-label={$t('fileDetail.cancelTitle')}
            title={$t('fileDetail.cancelTitle')}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>
      </div>
    {:else}
      <div class="title-display-container">
        <h1>{getDisplayName(file)}</h1>
        {#if sharedPermission}
          <span class="shared-chip">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
              <circle cx="9" cy="7" r="4"/>
              <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
              <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
            </svg>
            {$t('sharing.sharedChip', { permission: sharedPermission })}
          </span>
        {/if}
        <button
          type="button"
          on:click={startEditingTitle}
          class="edit-btn"
          title={$demoMode ? $t('demo.readOnlyTooltip') : $frozen ? $t('holds.frozenControl') : $t('fileDetail.editDisplayName')}
          aria-label={$t('fileDetail.editDisplayName')}
          disabled={$frozen || $demoMode}
          aria-describedby={$frozen ? 'held-file-banner-text' : undefined}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="m18 2 4 4-14 14H4v-4z"></path>
            <path d="m14.5 5.5 4 4"></path>
          </svg>
        </button>
      </div>
    {/if}
  </div>

  <DemoAttribution fileUuid={file?.uuid ? String(file.uuid) : null} />

  <HeldFileBanner hold={file?.hold} />

  <!-- Processing Status Display -->
  {#if file?.status === 'error'}
    <div class="status-message error" role="alert">
      <p class="error-heading"><strong>{$t('fileDetail.errorTitle')}</strong></p>
      <p>{mediaErrorI18nKey ? $t(mediaErrorI18nKey) : file.user_message || $t('fileDetail.errorGeneric')}</p>
      {#if file.error_suggestions?.length}
        <p class="error-suggestions-title">{$t('fileDetail.errorSuggestionsTitle')}</p>
        <ul class="error-suggestions">
          {#each file.error_suggestions as suggestion, i (i)}<li>{suggestion}</li>{/each}
        </ul>
      {/if}
      {#if file.is_retryable}<p class="error-retryable">{$t('fileDetail.errorRetryable')}</p>{/if}
    </div>
  {/if}

  {#if file?.status === 'processing' || file?.status === 'pending'}
    <div class="processing-info">
      <div class="processing-header">
        <div class="processing-icon">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"></circle>
            <polyline points="12 6 12 12 16 14"></polyline>
          </svg>
        </div>
        <div class="processing-status">
          <span class="processing-label">{$t('fileDetail.processingInProgress')}</span>
          <span class="processing-step-text">{#if currentProcessingStep}{currentProcessingStep}{:else}{$t('fileDetail.processingFile')}{/if}</span>
        </div>
        <span class="progress-percentage" dir="ltr">{file.progress || 0}%</span>
      </div>

      <ProgressBar
        percent={file.progress || 0}
        ariaLabel={$t('fileDetail.processingInProgress')}
      />

      <div class="processing-stages">
        <div class="stage {(file.progress || 0) >= 5 ? 'active' : ''} {(file.progress || 0) >= 25 ? 'completed' : ''}">
          <span class="stage-dot"></span>
          <span class="stage-label">{$t('fileDetail.stageSetup')}</span>
        </div>
        <div class="stage {(file.progress || 0) >= 25 ? 'active' : ''} {(file.progress || 0) >= 65 ? 'completed' : ''}">
          <span class="stage-dot"></span>
          <span class="stage-label">{$t('fileDetail.stageTranscription')}</span>
        </div>
        <div class="stage {(file.progress || 0) >= 65 ? 'active' : ''} {(file.progress || 0) >= 85 ? 'completed' : ''}">
          <span class="stage-dot"></span>
          <span class="stage-label">{$t('fileDetail.stageAnalysis')}</span>
        </div>
        <div class="stage {(file.progress || 0) >= 85 ? 'active' : ''} {(file.progress || 0) >= 100 ? 'completed' : ''}">
          <span class="stage-dot"></span>
          <span class="stage-label">{$t('fileDetail.stageFinalization')}</span>
        </div>
      </div>
    </div>
  {/if}


  {#if file?.description}
    <div class="file-summary">

      {#if shouldTruncate(file.description)}
        {#if isExpanded}
          <p class="summary-text">
            {file.description}
            <span class="expand-link" on:click={toggleExpanded} on:keydown={(e) => e.key === 'Enter' && toggleExpanded()} tabindex="0" role="button"> {$t('fileDetail.seeLess')}</span>
          </p>
        {:else}
          <div class="truncated-wrapper">
            <p class="summary-text truncated">{file.description}</p>
            <span class="expand-link" on:click={toggleExpanded} on:keydown={(e) => e.key === 'Enter' && toggleExpanded()} tabindex="0" role="button">{$t('fileDetail.seeMore')}</span>
          </div>
        {/if}
      {:else}
        <p class="summary-text">
          {file.description}
        </p>
      {/if}
    </div>
  {/if}
</div>

<!-- Modal dialogs -->
<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel}
  variant={$confirmVariant}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .edit-btn {
    background: none;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    padding: var(--space-2);
    border-radius: var(--radius-md);
    transition: all var(--duration-base) ease;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    margin-bottom: var(--space-4);
  }

  .edit-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .file-header-main {
    margin-bottom: var(--space-6);
  }

  .file-title h1 {
    margin: 0 0 var(--space-4) 0;
    font-size: 28px;
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
    line-height: 1.2;
    overflow-wrap: anywhere;
  }

  .status-message {
    margin: var(--space-4) 0;
    padding: var(--space-3) var(--space-4);
    border-radius: var(--radius-lg);
    border-inline-start: 4px solid;
  }

  .status-message.error {
    background: var(--error-light);
    border-color: var(--error-color);
    color: var(--error-dark);
  }

  .status-message p {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
  }

  .status-message .error-heading {
    margin-bottom: var(--space-1);
  }

  .status-message .error-suggestions-title {
    margin-top: var(--space-2);
    font-weight: var(--font-weight-semibold);
  }

  .status-message .error-suggestions {
    margin: var(--space-1) 0 0;
    padding-inline-start: var(--space-5);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-regular);
  }

  .status-message .error-suggestions li {
    margin: var(--space-0-5) 0;
  }

  .status-message .error-retryable {
    margin-top: var(--space-2);
    font-weight: var(--font-weight-regular);
    font-style: italic;
  }

  .processing-info {
    margin: var(--space-4) 0;
    padding: var(--space-5);
    background: var(--info-light);
    border: 1px solid var(--info-color);
    border-radius: var(--radius-2xl);
  }

  .processing-header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
  }

  .processing-icon {
    color: var(--primary-on-surface);
    animation: spin 2s linear infinite;
    flex-shrink: 0;
  }

  .processing-status {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
  }

  .processing-label {
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    color: var(--info-dark);
    line-height: 1.2;
  }

  .processing-step-text {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    font-style: italic;
    line-height: 1.2;
  }

  .progress-percentage {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--primary-on-surface);
    background: rgba(var(--primary-color-rgb), 0.1);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
    flex-shrink: 0;
  }


  .processing-stages {
    display: flex;
    justify-content: space-between;
    margin-top: var(--space-4);
    padding-top: var(--space-4);
    border-top: 1px solid var(--border-color);
  }

  .stage {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-1-5);
    flex: 1;
    position: relative;
  }

  .stage:not(:last-child)::after {
    content: '';
    position: absolute;
    top: 8px;
    left: calc(50% + 12px);
    right: calc(-50% + 12px);
    height: 2px;
    background: var(--border-color);
    z-index: var(--z-base);
  }

  .stage.active:not(:last-child)::after {
    background: var(--primary-color);
  }

  .stage-dot {
    width: 16px;
    height: 16px;
    border-radius: var(--radius-round);
    background: var(--border-color);
    border: 2px solid var(--surface-color);
    position: relative;
    z-index: 2;
    transition: all var(--duration-slow) ease;
  }

  .stage.active .stage-dot {
    background: var(--primary-color);
    animation: pulse 1.5s infinite;
  }

  .stage.completed .stage-dot {
    background: var(--success-color);
    animation: none;
  }

  @keyframes pulse {
    0%, 100% {
      transform: scale(1);
      opacity: 1;
    }
    50% {
      transform: scale(1.1);
      opacity: 0.8;
    }
  }

  .stage-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    text-align: center;
  }

  .stage.active .stage-label {
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-semibold);
  }

  .stage.completed .stage-label {
    color: var(--success-text);
  }

  .file-summary {
    margin: var(--space-4) 0;
  }

  .summary-text {
    margin: 0;
    font-size: var(--font-size-base);
    color: var(--text-secondary);
    line-height: 1.5;
    transition: max-height var(--duration-slow) ease-in-out;
    overflow: hidden;
  }

  .truncated-wrapper {
    position: relative;
  }

  .summary-text.truncated {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    margin-bottom: 0;
    padding-right: 5rem;
  }

  .expand-link {
    color: var(--primary-on-surface);
    cursor: pointer;
    font-weight: var(--font-weight-medium);
    text-decoration: none;
    white-space: nowrap;
    transition: color var(--duration-base) ease;
  }

  .truncated-wrapper .expand-link {
    position: absolute;
    bottom: 0;
    right: 0;
    background: var(--bg-primary);
    padding-left: 0.5em;
    margin-left: 0.3em;
  }

  .expand-link:hover {
    color: var(--primary-color-dark);
    text-decoration: underline;
  }

  .expand-link:focus {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 2px;
    border-radius: var(--radius-xs);
  }


  /* Title editing styles */
  .title-display-container {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .title-display-container h1 {
    margin: 0 0 var(--space-4) 0;
    flex: 1;
  }

  .edit-btn:hover {
    background: var(--surface-hover);
    color: var(--primary-on-surface);
  }

  .edit-btn:focus {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 2px;
  }

  .shared-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-2xl);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    background: rgba(var(--accent-rgb), 0.1);
    color: var(--primary-on-surface);
    white-space: nowrap;
    flex-shrink: 0;
    margin-bottom: var(--space-4);
    text-transform: capitalize;
  }

  .title-edit-container {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    margin: 0 0 var(--space-4) 0;
    min-height: calc(28px * 1.2);
  }

  .title-input {
    flex: 1;
    margin: 0;
    font-size: 28px;
    font-weight: var(--font-weight-semibold);
  }

  .title-input:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .edit-buttons {
    display: flex;
    gap: var(--space-1);
    align-self: flex-start;
    margin-top: var(--space-2);
  }

  .edit-buttons .btn {
    justify-content: center;
    padding: var(--space-2) var(--space-3);
    height: 32px;
    box-sizing: border-box;
  }

  .spinning {
    animation: spin 1s linear infinite;
  }

  @media (max-width: 768px) {
    .file-title h1 {
      font-size: var(--font-size-2xl);
    }

    .summary-text {
      font-size: var(--font-size-md);
    }

    .expand-link {
      font-size: 0.9em;
    }

    .title-input {
      font-size: var(--font-size-2xl);
      padding: var(--space-1-5) var(--space-2);
    }

    .title-display-container {
      gap: var(--space-2);
    }

    .edit-buttons {
      flex-direction: column;
    }
  }
</style>
