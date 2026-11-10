<script lang="ts">
  /**
   * The file-detail player: the page's `<video>`/`<audio>` inside the composed Video.js player
   * (`components/player/MediaPlayer`), driven through the `PlayerHandle` seam (#1327).
   *
   * Public API, unchanged for `fileDetail/FilePlayer` and the page: the props below, the
   * `timeupdate`/`loadedmetadata`/`play`/`pause`/`ended`/`seeking`/`seeked`/`retry` events (all
   * from native media events), `seekToTime(t)` and `updateSubtitles()`.
   */
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import { fetchCaptionTrack } from '$lib/api/captions';
  import { applyMediaSeek, waitForMediaMetadata, HAVE_METADATA } from '$lib/utils/mediaReady';
  import { playableSourceType } from '$lib/utils/mediaType';
  import { createVideojsPlayer } from '$lib/player/videojsPlayer';
  import { listenToMedia, mediaDuration } from '$lib/player/mediaEvents';
  import type { OwnedPlayer } from '$lib/player/types';
  import type { PlaybackMode } from '$lib/api/mediaUrl';
  import type { MediaFileDetail } from '$lib/types/media';
  import MediaPlayer from './player/MediaPlayer.svelte';
  import Spinner from './ui/Spinner.svelte';

  export let videoUrl: string = '';
  // The type /stream-url reports for videoUrl. It differs from file.content_type when
  // the URL serves a playback rendition (AAC/M4A) instead of the original.
  export let streamContentType: string = '';
  export let playbackMode: PlaybackMode | null = null;
  export let file: Partial<MediaFileDetail> | null = null;
  export let isPlayerBuffering: boolean = false;
  export let loadProgress: number = 0;
  export let errorMessage: string = '';
  // The owner/admin revealed the original on the page; the caption track follows that view.
  export let showOriginal: boolean = false;

  const dispatch = createEventDispatcher();

  let player: OwnedPlayer | null = null;
  let stopListening: (() => void) | null = null;
  let mediaElement: HTMLVideoElement | HTMLAudioElement | null = null;
  let duration = 0;
  let currentSubtitleBlobUrl: string | null = null;
  let isSeeking = false;
  // URL currently loaded into the media element — lets a presigned-URL refresh
  // hot-swap the credential without losing playback position (see hotSwapVideoSource).
  let activeSrc = '';

  // A stored alias such as audio/vnd.wave is rejected as a hint, and the browser then
  // never fetches the bytes (issue #1044). Without a hint it sniffs them instead.
  $: servedType = streamContentType || file?.content_type;
  $: sourceType = playableSourceType(servedType);
  $: playsAsAudio = playbackMode === 'audio_only' || !!servedType?.startsWith('audio/');

  function handleRetry() {
    dispatch('retry');
  }

  // Monotonic id of the most recent seek request. A late-resolving wait belonging
  // to a superseded seek must not overwrite the newer target.
  let seekRequestId = 0;
  let seekSpinnerTimer: ReturnType<typeof setTimeout> | null = null;

  function scheduleSpinnerFallback(ms: number) {
    if (seekSpinnerTimer !== null) clearTimeout(seekSpinnerTimer);
    seekSpinnerTimer = setTimeout(() => {
      seekSpinnerTimer = null;
      isSeeking = false;
    }, ms);
  }

  function clearSeekSpinner() {
    if (seekSpinnerTimer !== null) {
      clearTimeout(seekSpinnerTimer);
      seekSpinnerTimer = null;
    }
    isSeeking = false;
  }

  /**
   * Seek for the waveform, transcript, comment chips and the `?t=` deep link.
   *
   * Uses the bound element, not the player: a deep link can arrive in the same flush as this
   * component's mount, before the player exists (#649). The seek is written to the element
   * before metadata, which the browser keeps as the start position and fetches at once (#647).
   * The player's controls read the element, so they follow with no resync (plan row 8).
   */
  export async function seekToTime(time: number) {
    const media = mediaElement ?? player?.media;
    if (!media) {
      return;
    }

    const requestId = ++seekRequestId;
    isSeeking = true;
    scheduleSpinnerFallback(30000);

    const appliedDirectly = applyMediaSeek(media, time);
    dispatch('timeupdate', { currentTime: time, duration });

    if (!appliedDirectly && media.readyState < HAVE_METADATA) {
      const ready = await waitForMediaMetadata(media);
      if (requestId !== seekRequestId || !ready) return;
      applyMediaSeek(media, time);
    }
  }

  // Monotonic id of the latest caption fetch: a slow response must not overwrite a newer one.
  let captionsRequestId = 0;
  let captionsShownOriginal: boolean | null = null;

  /**
   * (Re)load the caption track from the server: when the player is ready, after a transcript
   * or speaker edit, and when the page toggles the redacted/original view.
   *
   * The cues come from `GET /files/{uuid}/captions` (see `$lib/api/captions`), cut into
   * broadcast-style cues there. When the server refuses (redaction scan still running,
   * policy unavailable) the track is emptied rather than left showing older cues. Whether the
   * track shows is the viewer's remembered choice (`$lib/player/prefs`), not decided here.
   */
  export async function updateSubtitles() {
    if (!player || !file?.uuid || file.status !== 'completed' || playsAsAudio) {
      return;
    }
    const activePlayer = player;
    const requestId = ++captionsRequestId;
    captionsShownOriginal = showOriginal;

    let webvtt = 'WEBVTT\n';
    try {
      webvtt = await fetchCaptionTrack({
        fileUuid: file.uuid,
        showOriginal,
        speakerPlaceholder: $t('speaker.localizedLabel', { number: '{number}' }),
      });
    } catch (error) {
      console.warn('Captions are unavailable for this file:', error);
    }
    if (requestId !== captionsRequestId || player !== activePlayer) {
      return;
    }

    const trackElement = activePlayer.media.querySelector<HTMLTrackElement>(
      'track[kind="captions"]'
    );
    if (!trackElement) {
      return;
    }
    const vttUrl = URL.createObjectURL(new Blob([webvtt], { type: 'text/vtt' }));
    if (currentSubtitleBlobUrl) {
      URL.revokeObjectURL(currentSubtitleBlobUrl);
    }
    currentSubtitleBlobUrl = vttUrl;
    trackElement.src = vttUrl;
  }

  $: if (player && captionsShownOriginal !== null && showOriginal !== captionsShownOriginal) {
    updateSubtitles();
  }

  function initializePlayer() {
    if (!mediaElement || player) {
      return;
    }

    const media = mediaElement;
    try {
      player = createVideojsPlayer(media, {
        variant: 'full',
        audio: playsAsAudio,
        onReady: () => {
          duration = mediaDuration(media);
          dispatch('loadedmetadata', { duration });
          if (file?.status === 'completed') {
            updateSubtitles();
          }
        },
      });
    } catch (error) {
      console.error('Error creating the media player:', error);
      return;
    }
    activeSrc = videoUrl;

    stopListening = listenToMedia(media, {
      timeupdate: () => {
        duration = mediaDuration(media);
        dispatch('timeupdate', { currentTime: media.currentTime, duration });
      },
      play: () => dispatch('play'),
      pause: () => dispatch('pause'),
      ended: () => dispatch('ended'),
      seeking: () => {
        isSeeking = true;
        dispatch('seeking');
      },
      seeked: () => {
        clearSeekSpinner();
        dispatch('seeked');
      },
      // A media error while seeking would otherwise leave the spinner up until the fallback.
      error: clearSeekSpinner,
    });
  }

  function destroyPlayer() {
    if (currentSubtitleBlobUrl) {
      URL.revokeObjectURL(currentSubtitleBlobUrl);
      currentSubtitleBlobUrl = null;
    }
    stopListening?.();
    stopListening = null;
    player?.destroy();
    player = null;
  }

  onMount(() => {
    if (videoUrl && mediaElement) {
      initializePlayer();
    }
  });

  onDestroy(() => {
    if (seekSpinnerTimer !== null) clearTimeout(seekSpinnerTimer);
    destroyPlayer();
  });

  // The served type can turn a <video> into an <audio> after mount; the old element's
  // player goes with it.
  $: if (player && mediaElement && player.media !== mediaElement) {
    destroyPlayer();
  }

  $: if (videoUrl && mediaElement && !player) {
    initializePlayer();
  }

  // Seamless presigned-URL refresh: reload the new credential but resume exactly
  // where we left off. Changing the <source> src alone does not reload the element.
  $: if (player && mediaElement && videoUrl && videoUrl !== activeSrc) {
    hotSwapVideoSource(videoUrl);
  }

  async function hotSwapVideoSource(newUrl: string) {
    activeSrc = newUrl;
    if (!player || !mediaElement) return;
    const activePlayer = player;
    const media = activePlayer.media;
    const resumeAt = media.currentTime;
    const wasPlaying = !media.paused && !media.ended;
    const sourceEl = media.querySelector('source');
    if (sourceEl) sourceEl.src = newUrl;
    media.load();
    await seekToTime(resumeAt);
    if (wasPlaying) {
      activePlayer.play().catch(() => {});
    }
  }
</script>

<div class="video-player-container">
  {#if videoUrl}
    {#if playbackMode === 'audio_only'}
      <p class="audio-only-notice" role="status">{$t('videoPlayer.audioOnlyPreview')}</p>
    {/if}
    <MediaPlayer audio={playsAsAudio} variant="full" media={mediaElement}>
      {#if playsAsAudio}
        <audio bind:this={mediaElement} id="player" preload="metadata" playsinline>
          <source src={videoUrl} type={sourceType} />
          {$t('videoPlayer.audioNotSupported')}
        </audio>
      {:else}
        <video bind:this={mediaElement} id="player" preload="metadata" playsinline>
          <source src={videoUrl} type={sourceType} />
          <!-- Always present, so the CC button shows before the cues arrive. -->
          <track kind="captions" label={$t('videoPlayer.captionsLabel')} srclang="en" />
          {$t('videoPlayer.videoNotSupported')}
        </video>
      {/if}
    </MediaPlayer>

    {#if isPlayerBuffering}
      <div class="buffer-indicator">
        <Spinner size="large" color="white" />
        <div class="buffer-text">
          {$t('videoPlayer.loadingProgress', { progress: Math.round(loadProgress) })}
        </div>
      </div>
    {/if}

    {#if isSeeking}
      <div class="seek-overlay">
        <Spinner label={$t('common.loading')} size="medium" color="white" />
      </div>
    {/if}

    {#if errorMessage}
      <p class="error-message">{errorMessage}</p>
      <button
        class="retry-button"
        on:click={handleRetry}
        title={$t('videoPlayer.retryLoadingTitle')}>{$t('videoPlayer.retryLoading')}</button
      >
    {/if}
  {:else}
    <div class="no-preview">
      {#if errorMessage}
        <p class="error-message">{errorMessage}</p>
        <button on:click={handleRetry} class="retry-button">{$t('videoPlayer.retryLoading')}</button>
      {:else}
        {$t('videoPlayer.notAvailable')}
      {/if}
    </div>
  {/if}
</div>

<style>
  .video-player-container {
    position: relative;
    width: 100%;
    border-radius: var(--radius-lg);
    background: var(--surface-color);
  }

  .buffer-indicator {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    text-align: center;
    color: var(--player-on-media);
    z-index: var(--z-sticky);
  }

  .buffer-text {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
  }

  .seek-overlay {
    position: absolute;
    inset: 0;
    background: var(--player-scrim);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: var(--z-sticky);
    pointer-events: none;
    border-radius: var(--radius-lg);
  }

  .error-message {
    color: var(--error-text);
    margin: var(--space-2-5) 0;
    text-align: center;
  }

  .audio-only-notice {
    margin: 0 0 var(--space-2);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
    background: var(--background-color);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    text-align: center;
  }

  .retry-button {
    background: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: var(--font-size-md);
    transition: background-color var(--duration-base);
  }

  .retry-button:hover {
    background: var(--primary-color-hover);
  }

  .no-preview {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 200px;
    background: var(--bg-color);
    border: 2px dashed var(--border-color);
    border-radius: var(--radius-lg);
    color: var(--text-muted);
  }
</style>
