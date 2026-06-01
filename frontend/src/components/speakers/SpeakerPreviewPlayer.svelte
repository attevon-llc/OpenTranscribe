<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fade } from 'svelte/transition';
  import { t } from '$stores/locale';
  import { browser } from '$app/environment';
  import type { SpeakerMediaPreviewData } from '$lib/api/speakerClusters';

  export let speakerPreviewData: SpeakerMediaPreviewData | null = null;
  export let previewCurrentTime = 0;
  export let playerRef: any = null;

  const dispatch = createEventDispatcher();

  // Dynamic import: Plyr is browser-only (breaks SSR on page refresh)
  let PlyrMiniPlayer: typeof import('$components/PlyrMiniPlayer.svelte').default | null = null;
  if (browser) {
    import('$components/PlyrMiniPlayer.svelte').then(m => { PlyrMiniPlayer = m.default; });
  }

  function formatPlaybackTime(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  }
</script>

<!-- Sticky Floating Preview Player -->
{#if speakerPreviewData}
  <div class="sticky-preview" transition:fade={{ duration: 150 }}>
    <div class="preview-header">
      <div class="preview-info">
        <span class="preview-title">
          {speakerPreviewData.speaker_name}
        </span>
        <span class="preview-playback-info">
          <span class="preview-time">{formatPlaybackTime(previewCurrentTime)}</span>
          <span class="preview-separator">|</span>
          <span class="preview-file-name">{speakerPreviewData.file_name}</span>
        </span>
      </div>
      <div class="preview-actions">
        <a class="preview-detail-link" href="/files/{speakerPreviewData.file_uuid}?t={previewCurrentTime || speakerPreviewData.start_time}">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
            <polyline points="15 3 21 3 21 9"></polyline>
            <line x1="10" y1="14" x2="21" y2="3"></line>
          </svg>
          {$t('search.jumpTo')}
        </a>
        <button class="preview-close" on:click={() => dispatch('close')} title={$t('speakers.preview.close')} aria-label={$t('speakers.preview.closeAriaLabel')}>
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
    </div>
    <div class="preview-player-container">
      {#key `${speakerPreviewData?.media_url}:${speakerPreviewData?.start_time}`}
        {#if PlyrMiniPlayer && speakerPreviewData}
          <svelte:component this={PlyrMiniPlayer}
            bind:this={playerRef}
            mediaUrl={speakerPreviewData.media_url || ''}
            contentType={speakerPreviewData.content_type}
            startTime={speakerPreviewData.start_time}
            endTime={speakerPreviewData.end_time}
            autoplay={true}
            fileId={speakerPreviewData.file_uuid}
            compact={true}
            on:timeupdate
            on:play
            on:pause
          />
        {/if}
      {/key}
    </div>
  </div>
{/if}

<style>
  /* Sticky Floating Preview Player */
  .sticky-preview {
    position: fixed;
    bottom: 1rem;
    right: 1rem;
    width: 400px;
    max-width: calc(100vw - 2rem);
    background: var(--surface-color, #fff);
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.15), 0 2px 8px rgba(0, 0, 0, 0.08);
    z-index: 1000;
    overflow: hidden;
  }

  .preview-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.5rem 0.75rem;
    background: var(--surface-color, #f9fafb);
    border-bottom: 1px solid var(--border-color, #e5e7eb);
  }

  .preview-info {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    min-width: 0;
    flex: 1;
  }

  .preview-title {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color, #111827);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .preview-playback-info {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    font-size: 0.75rem;
  }

  .preview-time {
    font-family: monospace;
    font-weight: 600;
    color: var(--primary-color, #4f46e5);
  }

  .preview-separator {
    color: var(--text-secondary, #9ca3af);
  }

  .preview-file-name {
    color: var(--text-secondary, #6b7280);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .preview-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
    margin-left: 0.5rem;
  }

  .preview-detail-link {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    font-size: 0.75rem;
    color: var(--primary-color, #4f46e5);
    text-decoration: none;
    padding: 0.25rem 0.5rem;
    border-radius: 6px;
    transition: background 0.15s;
  }

  .preview-detail-link:hover {
    background: color-mix(in srgb, var(--primary-color, #4f46e5) 8%, transparent);
  }

  .preview-close {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
    transition: color 0.2s ease, background 0.2s ease;
  }

  .preview-close:hover {
    color: var(--text-color);
    background: var(--button-hover, var(--background-color));
  }

  @media (max-width: 768px) {
    .sticky-preview {
      left: 0.5rem;
      right: 0.5rem;
      bottom: 0.5rem;
      width: auto;
      max-width: none;
    }

    .preview-header {
      padding: 0.375rem 0.5rem;
    }

    .preview-detail-link {
      font-size: 0.6875rem;
      padding: 0.25rem 0.375rem;
    }
  }
</style>
