<script context="module" lang="ts">
  /**
   * `idle`: this occurrence is not the one in the floating player.
   * `playing` / `paused`: it is, and the player reports that state (Plyr `play`/`pause`;
   * `ended` also reports `pause`). `paused` covers a clip whose autoplay was blocked.
   */
  export type PreviewPlaybackState = 'idle' | 'playing' | 'paused';
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let state: PreviewPlaybackState = 'idle';

  const dispatch = createEventDispatcher<{ click: void }>();

  $: label =
    state === 'playing'
      ? $t('search.pausePreview')
      : state === 'paused'
        ? $t('search.resumePreview')
        : $t('search.previewClip');
</script>

<button
  type="button"
  class="preview-btn"
  class:active={state !== 'idle'}
  class:playing={state === 'playing'}
  aria-pressed={state === 'playing'}
  aria-label={label}
  title={label}
  on:click={() => dispatch('click')}
>
  {#if state === 'playing'}
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <rect x="6" y="5" width="4" height="14" rx="1"></rect>
      <rect x="14" y="5" width="4" height="14" rx="1"></rect>
    </svg>
  {:else}
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M8 5.14v13.72a1 1 0 0 0 1.5.86l11.04-6.86a1 1 0 0 0 0-1.72L9.5 4.28a1 1 0 0 0-1.5.86z"></path>
    </svg>
  {/if}
</button>

<style>
  /* Primary-coloured round control: outlined when idle, filled when this clip is loaded.
     Never --error-color: red read as "stop recording" / an error, not as playback. */
  .preview-btn {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0;
    border: 1px solid var(--border-color);
    border-radius: 50%;
    background: var(--surface-color);
    box-shadow: none;
    color: var(--primary-on-surface);
    cursor: pointer;
    transition:
      background-color 0.15s,
      border-color 0.15s,
      color 0.15s;
  }

  .preview-btn:hover {
    border-color: var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.1);
    box-shadow: none;
    transform: none;
  }

  .preview-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .preview-btn.active {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: #fff;
  }

  /* A ring, not a darker fill: --primary-color-dark aliases --primary-on-surface, which is a
     LIGHT blue in dark mode and would wash out the white glyph. */
  .preview-btn.active:hover {
    background: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(var(--primary-color-rgb), 0.25);
  }
</style>
