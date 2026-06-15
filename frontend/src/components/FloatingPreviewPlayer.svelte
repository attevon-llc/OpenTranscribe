<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { formatClock } from '$lib/utils/formatting';
  import PlyrMiniPlayer from '$components/PlyrMiniPlayer.svelte';

  // Display
  export let title: string;
  export let subtitle: string = '';
  export let currentTime: number = 0;
  export let jumpToHref: string;
  export let closeTitle: string;
  export let closeAriaLabel: string;

  // Media (passed through to PlyrMiniPlayer)
  export let mediaUrl: string;
  export let contentType: string;
  export let startTime: number = 0;
  export let endTime: number = 0;
  export let fileId: string = '';
  export let autoplay: boolean = true;

  const dispatch = createEventDispatcher();

  let innerPlayer: PlyrMiniPlayer | null = null;

  $: isAudio = contentType?.startsWith('audio/') ?? false;

  // Re-expose the inner player so coordinators can pause/seek it. Keyed on
  // fileId:startTime (see {#key}); the ref is rebound on each new preview.
  export function getPlayer() {
    return innerPlayer?.getPlayer() ?? null;
  }

  export function seek(time: number) {
    innerPlayer?.seek(time);
  }
</script>

<!-- Sticky Floating Preview Player -->
<div class="sticky-preview">
  <div class="preview-header">
    <div class="preview-info">
      <span class="preview-title">
        {#if isAudio}
          <svg class="preview-media-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
            <line x1="12" y1="19" x2="12" y2="23"></line>
            <line x1="8" y1="23" x2="16" y2="23"></line>
          </svg>
        {:else}
          <svg class="preview-media-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="23 7 16 12 23 17 23 7"></polygon>
            <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
          </svg>
        {/if}
        {title}
      </span>
      <span class="preview-playback-info">
        <span class="preview-time">{formatClock(currentTime)}</span>
        {#if subtitle}
          <span class="preview-separator">|</span>
          <span class="preview-subtitle">{subtitle}</span>
        {/if}
      </span>
    </div>
    <div class="preview-actions">
      <a class="preview-detail-link" href={jumpToHref}>
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
          <polyline points="15 3 21 3 21 9"></polyline>
          <line x1="10" y1="14" x2="21" y2="3"></line>
        </svg>
        {$t('search.jumpTo')}
      </a>
      <button class="preview-close" on:click={() => dispatch('close')} title={closeTitle} aria-label={closeAriaLabel}>
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>
  </div>
  <div class="preview-player-container">
    {#key `${fileId}:${startTime}`}
      <PlyrMiniPlayer
        bind:this={innerPlayer}
        {mediaUrl}
        {contentType}
        {startTime}
        {endTime}
        {autoplay}
        {fileId}
        compact={true}
        on:timeupdate
        on:play
        on:pause
      />
    {/key}
  </div>
</div>

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
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
  }

  .preview-media-icon {
    flex-shrink: 0;
    opacity: 0.7;
  }

  .preview-playback-info {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    font-size: 0.75rem;
    min-width: 0;
  }

  .preview-time {
    font-family: monospace;
    font-weight: 600;
    color: var(--primary-color, #4f46e5);
    flex-shrink: 0;
    white-space: nowrap;
  }

  .preview-separator {
    color: var(--text-secondary, #9ca3af);
    flex-shrink: 0;
  }

  .preview-subtitle {
    color: var(--text-secondary, #6b7280);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
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
      width: calc(100vw - 1rem);
      right: 0.5rem;
      bottom: 0.5rem;
    }

    .preview-header {
      flex-wrap: wrap;
      gap: 0.25rem;
    }

    .preview-actions {
      margin-left: 0;
    }
  }
</style>
