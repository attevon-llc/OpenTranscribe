<script lang="ts">
  /**
   * The file page's media: the player and, for a completed audio/video file, the waveform.
   * It keeps the playhead (`bind:currentTime`) and the transcript's active-segment highlight
   * in step, and performs the one-time `?t=` seek the page hands it as `initialSeekTime`.
   *
   * The page owns the stream URL (it is resolved while the page is still loading, before this
   * component exists) and calls `seekToTime` / `updateSubtitles` through `bind:this`.
   */
  import { afterUpdate, createEventDispatcher } from 'svelte';
  import VideoPlayer from '$components/VideoPlayer.svelte';
  import WaveformPlayer from '$components/WaveformPlayer.svelte';
  import type { PlaybackMode } from '$lib/api/mediaUrl';
  import { markActiveSegment, scrollToSegmentAtTime, type Schedule } from '$lib/fileDetail/segmentDom';
  import type { MediaFileDetail } from '$lib/types/media';

  export let file: MediaFileDetail & { duration_seconds?: number | null };
  export let videoUrl = '';
  export let streamContentType = '';
  export let playbackMode: PlaybackMode | null = null;
  export let errorMessage = '';
  export let showOriginal = false;
  /** The page's cancellable `setTimeout` (`pageTimers.schedule`). */
  export let schedule: Schedule;
  export let currentTime = 0;
  /** The `?t=` deep-link time, consumed once (`initialSeekDone`); `null` when there is none. */
  export let initialSeekTime: number | null = null;

  const dispatch = createEventDispatcher<{ retry: void; initialSeekDone: void }>();

  let videoPlayerComponent: VideoPlayer | null = null;
  // True once the media element exists; the `?t=` seek waits for it.
  let playerInitialized = false;

  $: hasWaveform =
    !!file?.uuid &&
    (file.content_type?.startsWith('audio/') || file.content_type?.startsWith('video/')) &&
    file.status === 'completed';

  /** Seek with 0.5 s of lead-in, for context. */
  export function seekToTime(time: number) {
    const paddedTime = Math.max(0, time - 0.5);
    if (videoPlayerComponent && videoPlayerComponent.seekToTime) {
      videoPlayerComponent.seekToTime(paddedTime);
    } else {
      console.warn('VideoPlayer component not available for seeking');
    }
  }

  /** Re-fetch the server-built caption track. */
  export async function updateSubtitles() {
    await videoPlayerComponent?.updateSubtitles?.();
  }

  function initializePlayer() {
    if (playerInitialized || !videoUrl) return;
    // All player initialization is handled by VideoPlayer; this only notes the element exists.
    if (!document.querySelector('#player')) return;
    playerInitialized = true;
  }

  function handleTimeUpdate(event: CustomEvent<{ currentTime: number }>) {
    currentTime = event.detail.currentTime;
    markActiveSegment(file?.transcript_segments, currentTime);
  }

  // Initialize as soon as the element exists, with no sleep: one only delayed the `?t=`
  // seek that follows (#649).
  afterUpdate(() => {
    if (videoUrl && !playerInitialized && document.getElementById('player')) {
      initializePlayer();
    }
  });

  // And retry shortly after a new URL, in case the player renders late.
  $: if (videoUrl && !playerInitialized) {
    schedule(() => {
      if (!playerInitialized) initializePlayer();
    }, 200);
  }

  $: if (playerInitialized && videoPlayerComponent && initialSeekTime !== null) {
    seekFromDeepLink(initialSeekTime);
  }

  // `VideoPlayer.seekToTime` issues the seek before metadata arrives (#647), so this needs
  // no delay of its own.
  function seekFromDeepLink(targetTime: number) {
    dispatch('initialSeekDone');
    if (videoPlayerComponent && typeof videoPlayerComponent.seekToTime === 'function') {
      videoPlayerComponent.seekToTime(targetTime);
    } else {
      currentTime = targetTime;
    }
    scrollToSegmentAtTime(file?.transcript_segments, targetTime, schedule);
  }
</script>

<VideoPlayer
  bind:this={videoPlayerComponent}
  {videoUrl}
  {streamContentType}
  {playbackMode}
  {file}
  {errorMessage}
  {showOriginal}
  on:retry={() => dispatch('retry')}
  on:timeupdate={handleTimeUpdate}
/>

<!-- Waveform visualization -->
{#if hasWaveform}
  <div class="waveform-section">
    <WaveformPlayer
      fileId={file.uuid}
      duration={file.duration_seconds || file.duration || 0}
      {currentTime}
      height={80}
      on:seek={(event) => seekToTime(event.detail.time)}
    />
  </div>
{/if}

<style>
  .waveform-section {
    inline-size: 100%;
  }
</style>
