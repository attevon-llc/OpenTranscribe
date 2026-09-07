<script lang="ts">
  import type { MediaFileDetail } from '$lib/types/media';
  import { createEventDispatcher } from 'svelte';
  import axiosInstance from '$lib/axios';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import { t } from '$stores/locale';

  const dispatch = createEventDispatcher();

  export let file: MediaFileDetail | null = null;
  export let currentProcessingStep: string = '';
  export let sharedPermission: string | null = null;

  let isExpanded = false;
  let isEditingTitle = false;
  let editedTitle = '';
  let isSaving = false;

  // Modal state
  let showLengthErrorModal = false;
  let showSaveErrorModal = false;

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
      showLengthErrorModal = true;
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
      showSaveErrorModal = true;
    } finally {
      isSaving = false;
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      saveTitle();
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
            class="save-btn"
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
            class="cancel-btn"
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
          title={$t('fileDetail.editDisplayName')}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="m18 2 4 4-14 14H4v-4z"></path>
            <path d="m14.5 5.5 4 4"></path>
          </svg>
        </button>
      </div>
    {/if}
  </div>

  <!-- Processing Status Display -->
  {#if file?.status === 'error' && file?.error_message}
    <div class="status-message error">
      <p><strong>{$t('fileDetail.processingStatus')}:</strong> {file.error_message}</p>
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
        <span class="progress-percentage">{file.progress || 0}%</span>
      </div>

      <div class="progress-bar">
        <div class="progress-bar-inner" style="width: {file.progress || 0}%;"></div>
      </div>

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
  bind:isOpen={showLengthErrorModal}
  title={$t('fileDetail.displayNameTooLong')}
  message={$t('fileDetail.displayNameTooLongMessage')}
  confirmText={$t('common.confirm')}
  cancelText=""
  confirmButtonClass="confirm-button"
  on:confirm={() => showLengthErrorModal = false}
  on:close={() => showLengthErrorModal = false}
/>

<ConfirmationModal
  bind:isOpen={showSaveErrorModal}
  title={$t('fileDetail.saveFailed')}
  message={$t('fileDetail.saveFailedMessage')}
  confirmText={$t('common.confirm')}
  cancelText=""
  confirmButtonClass="confirm-button"
  on:confirm={() => showSaveErrorModal = false}
  on:close={() => showSaveErrorModal = false}
/>

<style>
  .file-header-main {
    margin-bottom: 24px;
  }

  .file-title h1 {
    margin: 0 0 16px 0;
    font-size: 28px;
    font-weight: 700;
    color: var(--text-primary);
    line-height: 1.2;
    word-break: break-word;
  }

  .status-message {
    margin: 16px 0;
    padding: 12px 16px;
    border-radius: 8px;
    border-left: 4px solid;
  }

  .status-message.error {
    background: var(--error-light);
    border-color: var(--error-color);
    color: var(--error-dark);
  }

  .status-message p {
    margin: 0;
    font-size: 14px;
    font-weight: 500;
  }

  .processing-info {
    margin: 16px 0;
    padding: 20px;
    background: var(--info-light);
    border: 1px solid var(--info-color);
    border-radius: 12px;
  }

  .processing-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
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
    gap: 2px;
  }

  .processing-label {
    font-size: 16px;
    font-weight: 600;
    color: var(--info-dark);
    line-height: 1.2;
  }

  .processing-step-text {
    font-size: 14px;
    font-weight: 500;
    color: var(--text-secondary);
    font-style: italic;
    line-height: 1.2;
  }

  .progress-percentage {
    font-size: 14px;
    font-weight: 700;
    color: var(--primary-on-surface);
    background: rgba(var(--primary-color-rgb), 0.1);
    padding: 4px 8px;
    border-radius: 4px;
    flex-shrink: 0;
  }

  .progress-bar {
    width: 100%;
    height: 8px;
    background: var(--surface-color);
    border-radius: 4px;
    overflow: hidden;
    border: 1px solid var(--border-color);
  }

  .progress-bar-inner {
    height: 100%;
    background: linear-gradient(90deg, var(--primary-color), rgba(var(--primary-color-rgb), 0.8));
    border-radius: 4px;
    transition: width 0.3s ease;
    animation: progress-shimmer 1.5s infinite;
  }

  @keyframes progress-shimmer {
    0% {
      background-position: -200px 0;
    }
    100% {
      background-position: calc(200px + 100%) 0;
    }
  }

  .processing-stages {
    display: flex;
    justify-content: space-between;
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid var(--border-color);
  }

  .stage {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
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
    z-index: 1;
  }

  .stage.active:not(:last-child)::after {
    background: var(--primary-color);
  }

  .stage-dot {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--border-color);
    border: 2px solid var(--surface-color);
    position: relative;
    z-index: 2;
    transition: all 0.3s ease;
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
    font-size: 12px;
    font-weight: 500;
    color: var(--text-secondary);
    text-align: center;
  }

  .stage.active .stage-label {
    color: var(--primary-on-surface);
    font-weight: 600;
  }

  .stage.completed .stage-label {
    color: var(--success-color);
  }

  .file-summary {
    margin: 16px 0;
  }

  .summary-text {
    margin: 0;
    font-size: 16px;
    color: var(--text-secondary);
    line-height: 1.5;
    transition: max-height 0.3s ease-in-out;
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

  .truncated-wrapper .expand-link {
    position: absolute;
    bottom: 0;
    right: 0;
    background: var(--bg-primary);
    padding-left: 0.5em;
    margin-left: 0.3em;
  }

  .expand-link {
    color: var(--primary-on-surface);
    cursor: pointer;
    font-weight: 500;
    text-decoration: none;
    white-space: nowrap;
    transition: color 0.2s ease;
  }

  .expand-link:hover {
    color: var(--primary-color-dark, #2563eb);
    text-decoration: underline;
  }

  .expand-link:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 2px;
  }


  /* Title editing styles */
  .title-display-container {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .title-display-container h1 {
    margin: 0 0 16px 0;
    flex: 1;
  }

  .edit-btn {
    background: none;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    padding: 8px;
    border-radius: 6px;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    margin-bottom: 16px;
  }

  .edit-btn:hover {
    background: var(--surface-hover);
    color: var(--primary-on-surface);
  }

  .edit-btn:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .shared-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
    background: rgba(59, 130, 246, 0.1);
    color: var(--primary-on-surface);
    white-space: nowrap;
    flex-shrink: 0;
    margin-bottom: 16px;
    text-transform: capitalize;
  }

  :global([data-theme='dark']) .shared-chip {
    background: rgba(59, 130, 246, 0.15);
    color: #60a5fa;
  }

  .title-edit-container {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin: 0 0 16px 0;
    min-height: calc(28px * 1.2);
  }

  .title-input {
    flex: 1;
    padding: 8px 12px;
    margin: 0;
    font-size: 28px;
    font-weight: 700;
    line-height: 1.2;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-primary);
    transition: border-color 0.2s ease;
    font-family: inherit;
  }

  .title-input:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .title-input:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .edit-buttons {
    display: flex;
    gap: 4px;
    align-self: flex-start;
    margin-top: 8px;
  }

  .save-btn,
  .cancel-btn {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all 0.2s ease;
    border: 1px solid;
    height: 32px;
    box-sizing: border-box;
  }

  .save-btn {
    background-color: var(--success-color);
    border-color: var(--success-color);
    color: white;
    padding: 0.5rem 0.5rem;
  }

  .save-btn:hover:not(:disabled) {
    background-color: #059669;
    border-color: #059669;
  }

  .cancel-btn {
    background-color: #6b7280;
    border-color: #6b7280;
    color: white;
  }

  .cancel-btn:hover:not(:disabled) {
    background-color: #4b5563;
    border-color: #4b5563;
    color: white;
  }

  .save-btn:disabled,
  .cancel-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .save-btn:focus,
  .cancel-btn:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .spinning {
    animation: spin 1s linear infinite;
  }

  @media (max-width: 768px) {
    .file-title h1 {
      font-size: 24px;
    }

    .summary-text {
      font-size: 14px;
    }

    .expand-link {
      font-size: 0.9em;
    }

    .title-input {
      font-size: 24px;
      padding: 6px 8px;
    }

    .title-display-container {
      gap: 8px;
    }

    .edit-buttons {
      flex-direction: column;
    }
  }
</style>
