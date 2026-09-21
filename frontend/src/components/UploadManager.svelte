<script lang="ts">
  import {
    uploadsStore,
    activeUploadCount,
    uploadCount,
    totalProgress,
    hasActiveUploads,
    isExpanded,
    hasNewActivity,
    uploadStats
  } from '../stores/uploads';
  import { t } from '../stores/locale';
  import UploadProgress from './UploadProgress.svelte';
  import EmptyState from './ui/EmptyState.svelte';

  // Toggle expanded state
  function handleToggle() {
    uploadsStore.toggle();
  }

  // Clear completed uploads
  function handleClearCompleted() {
    uploadsStore.clearCompleted();
  }

  // Handle keyboard shortcuts
  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && $isExpanded) {
      uploadsStore.collapse();
    }
  }

  // Reactive values
  $: showManager = $uploadCount > 0;
  $: completedCount = $uploadStats.completed;
</script>

<svelte:window on:keydown={handleKeydown} />

{#if showManager}
  <div
    class="upload-manager"
    class:expanded={$isExpanded}
  >
    <!-- Minimized State -->
    {#if !$isExpanded}
      <div
        class="upload-badge"
        class:has-activity={$hasNewActivity}
        on:click={handleToggle}
        role="button"
        tabindex="0"
        on:keydown={(e) => e.key === 'Enter' && handleToggle()}
      >
        <div class="badge-content">
          <div class="upload-icon">
            {#if $hasActiveUploads}
              <!-- Proper spinner when uploading -->
              <svg class="spinner" width="16" height="16" viewBox="0 0 24 24" fill="none">
                <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-dasharray="15.708" stroke-dashoffset="15.708">
                  <animate attributeName="stroke-dasharray" dur="1s" values="0 31.416;15.708 15.708;0 31.416" repeatCount="indefinite"/>
                  <animate attributeName="stroke-dashoffset" dur="1s" values="0;-15.708;-31.416" repeatCount="indefinite"/>
                </circle>
              </svg>
            {:else}
              <!-- Static upload icon when not uploading -->
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <polyline points="17 8 12 3 7 8"></polyline>
                <line x1="12" y1="3" x2="12" y2="15"></line>
              </svg>
            {/if}
          </div>

          <div class="badge-text" aria-live="polite">
            {#if $activeUploadCount > 0}
              {$activeUploadCount} {$t('upload.uploading')}
            {:else if completedCount > 0}
              {completedCount} {$t('upload.completed')}
            {:else}
              {$uploadCount} {$t('upload.uploads')}
            {/if}
          </div>


          {#if $hasNewActivity}
            <div class="activity-indicator"></div>
          {/if}
        </div>
      </div>
    {/if}

    <!-- Expanded State -->
    {#if $isExpanded}
      <div class="upload-panel">
        <!-- Header -->
        <div class="panel-header">
          <div class="header-content">
            <h3>{$t('upload.uploads')}</h3>
            <div class="header-stats">
              {#if $activeUploadCount > 0}
                <span class="stat-active">{$activeUploadCount} {$t('upload.active')}</span>
              {/if}
              {#if $uploadStats.queued > 0}
                <span class="stat-queued">{$uploadStats.queued} {$t('upload.queued')}</span>
              {/if}
              {#if completedCount > 0}
                <span class="stat-completed">{completedCount} {$t('upload.completed')}</span>
              {/if}
            </div>
          </div>

          <div class="header-actions">
            {#if completedCount > 0}
              <button
                class="clear-btn"
                on:click={handleClearCompleted}
                title={$t('upload.clearCompletedTooltip')}
              >
                {$t('upload.clear')}
              </button>
            {/if}

            <button
              class="collapse-btn"
              on:click={handleToggle}
              title={$t('upload.minimize')}
            >
              ▼
            </button>
          </div>
        </div>

        <!-- Overall Progress (if active uploads) -->
        {#if $hasActiveUploads}
          <div class="overall-progress">
            <div class="progress-info">
              <span class="progress-label">{$t('upload.overallProgress')}</span>
              <span class="progress-percent">{$totalProgress}%</span>
            </div>
            <div class="progress-bar">
              <div
                class="progress-fill"
                style="width: {$totalProgress}%"
              ></div>
            </div>
          </div>
        {/if}

        <!-- Upload List -->
        <div class="upload-list">
          {#each $uploadsStore.uploads as upload (upload.id)}
            <UploadProgress {upload} />
          {/each}
        </div>

        {#if $uploadCount === 0}
          <EmptyState title={$t('upload.noUploads')} padding="32px 16px">
            <svelte:fragment slot="icon">
              <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <polyline points="17 8 12 3 7 8"></polyline>
                <line x1="12" y1="3" x2="12" y2="15"></line>
              </svg>
            </svelte:fragment>
          </EmptyState>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  /*
   * Drive-style fixed corner tray — never draggable (#752 gap 3; the previous
   * drag math was inverted on both axes, had no touch equivalent, and fought
   * the "stays put" behaviour the issue actually asked for). Anchored with
   * logical properties (`inset-*-end`) rather than `right`/`bottom` so it
   * sits at the trailing corner in RTL locales too (#2.6 — every other
   * physically-anchored element in this app is a known RTL gap; new code
   * shouldn't add to it).
   */
  .upload-manager {
    position: fixed;
    inset-inline-end: 20px;
    inset-block-end: 20px;
    /* Sits in the documented --z-toast tier (theme.css) alongside Toast and
       the classification banner, one tier below --z-critical. */
    z-index: var(--z-toast);
    font-family: var(--font-family);
    user-select: none;
    margin-bottom: env(safe-area-inset-bottom, 0px);
  }

  /* Minimized Badge */
  .upload-badge {
    background: var(--primary-color);
    color: white;
    border-radius: 20px;
    padding: 8px 12px;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    transition: all 0.2s ease;
    position: relative;
    backdrop-filter: blur(8px);
  }

  .upload-badge:hover {
    transform: scale(1.05);
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.3);
  }

  .upload-badge.has-activity {
    animation: pulse-activity 2s ease-in-out infinite;
  }

  .badge-content {
    display: flex;
    align-items: center;
    gap: 6px;
    position: relative;
  }

  .upload-icon {
    display: flex;
    align-items: center;
  }

  .badge-text {
    font-size: 0.875rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .activity-indicator {
    position: absolute;
    top: -2px;
    inset-inline-end: -2px;
    width: 8px;
    height: 8px;
    background: var(--error-color);
    border-radius: 50%;
    border: 2px solid white;
    animation: pulse 2s ease-in-out infinite;
  }

  /* Expanded Panel */
  .upload-panel {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
    width: 350px;
    max-height: 60vh;
    overflow: hidden;
    backdrop-filter: blur(16px);
  }

  .panel-header {
    background: var(--primary-color);
    color: white;
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .header-content h3 {
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
  }

  .header-stats {
    display: flex;
    gap: 8px;
    font-size: 0.75rem;
    margin-top: 2px;
  }

  .stat-active {
    color: #fbbf24;
  }

  .stat-queued {
    color: #d1d5db;
  }

  .stat-completed {
    color: #86efac;
  }

  .header-actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .clear-btn,
  .collapse-btn {
    background: rgba(255, 255, 255, 0.2);
    border: none;
    color: white;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    cursor: pointer;
    transition: background 0.2s ease;
  }

  .clear-btn:hover,
  .collapse-btn:hover {
    background: rgba(255, 255, 255, 0.3);
  }

  /* Overall Progress */
  .overall-progress {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border-color);
    background: var(--background-color);
  }

  .progress-info {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
  }

  .progress-label {
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-primary);
  }

  .progress-percent {
    font-size: 0.875rem;
    color: var(--text-secondary);
  }

  .progress-bar {
    height: 6px;
    background: var(--border-color);
    border-radius: 3px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background: var(--primary-color);
    transition: width 0.3s ease;
  }

  /* Upload List */
  .upload-list {
    max-height: 40vh;
    overflow-y: auto;
    padding: 8px;
  }

  .upload-list::-webkit-scrollbar {
    width: 6px;
  }

  .upload-list::-webkit-scrollbar-track {
    background: var(--border-color);
    border-radius: 3px;
  }

  .upload-list::-webkit-scrollbar-thumb {
    background: var(--text-secondary);
    border-radius: 3px;
  }

  .upload-list::-webkit-scrollbar-thumb:hover {
    background: var(--text-primary);
  }

  /* Animations */

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  @keyframes pulse-activity {
    0%, 100% {
      transform: scale(1);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }
    50% {
      transform: scale(1.02);
      box-shadow: 0 6px 16px rgba(0, 0, 0, 0.3);
    }
  }

  /* Dark mode adjustments */
  :global([data-theme='dark']) .upload-panel {
    background: var(--surface-color);
    border-color: var(--border-color);
  }

  :global([data-theme='dark']) .overall-progress {
    background: var(--background-color);
    border-color: var(--border-color);
  }

  :global([data-theme='dark']) .progress-label {
    color: var(--text-primary);
  }

  :global([data-theme='dark']) .progress-percent {
    color: var(--text-secondary);
  }

  :global([data-theme='dark']) .progress-bar {
    background: var(--border-color);
  }

  /* Mobile adjustments */
  @media (max-width: 640px) {
    .upload-panel {
      width: calc(100vw - 40px);
      max-width: 350px;
    }

    .upload-manager {
      inset-inline: 20px !important;
      width: auto;
    }
  }
</style>
