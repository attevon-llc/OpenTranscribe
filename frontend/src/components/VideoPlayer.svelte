<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import Plyr from 'plyr';
  import 'plyr/dist/plyr.css';
  import { t } from '$stores/locale';
  import { fetchCaptionTrack } from '$lib/api/captions';
  import { applyMediaSeek, waitForMediaMetadata, HAVE_METADATA } from '$lib/utils/mediaReady';
  import { playableSourceType } from '$lib/utils/mediaType';
  import type { PlaybackMode } from '$lib/api/mediaUrl';
  import Spinner from './ui/Spinner.svelte';

  export let videoUrl: string = '';
  // The type /stream-url reports for videoUrl. It differs from file.content_type when
  // the URL serves a playback rendition (AAC/M4A) instead of the original.
  export let streamContentType: string = '';
  export let playbackMode: PlaybackMode | null = null;
  export let file: any = null;
  export let isPlayerBuffering: boolean = false;
  export let loadProgress: number = 0;
  export let errorMessage: string = '';
  // The owner/admin revealed the original on the page; the caption track follows that view.
  export let showOriginal: boolean = false;

  const dispatch = createEventDispatcher();

  let player: Plyr | null = null;
  let mediaElement: HTMLVideoElement | HTMLAudioElement;
  let currentTime = 0;
  let duration = 0;
  let currentSubtitleBlobUrl: string | null = null;
  let trackElement: HTMLTrackElement | null = null;
  let currentLoadHandler: ((event: Event) => void) | null = null;
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

  // External seek function for waveform and transcript clicks
  export async function seekToTime(time: number) {
    // Use the raw bound element, not `(player as any).media` — that requires
    // Plyr to have been constructed first, which is itself async (this
    // component's own `$: if (videoUrl && mediaElement && !player)
    // initializePlyr()`). A caller (e.g. the `?t=` deep-link handler) firing
    // in the same reactivity flush as this component's mount can land here
    // BEFORE that reactive statement has run, i.e. while `player` is still
    // null. The old code's `if (!player) return` silently dropped exactly
    // that seek — the same "act on not-yet-ready state" shape #649 found and
    // fixed in WaveformPlayer's click handler, here unmasked by removing the
    // caller's incidental delay rather than caused by it. `mediaElement` is
    // bound directly on the <video>/<audio> tag and needs no Plyr instance.
    const media = mediaElement ?? ((player as any)?.media as HTMLMediaElement | undefined);
    if (!media) {
      return;
    }

    const requestId = ++seekRequestId;

    // Show seeking spinner immediately, and keep it up until `seeked` fires
    // rather than clearing it on a blind short timer.
    isSeeking = true;
    scheduleSpinnerFallback(30000);

    // Issue the seek to the media element FIRST, without waiting for metadata.
    // Before `loadedmetadata` the assignment is not discarded — the element
    // records it as the default playback start position, so the browser fetches
    // the byte range for the target as soon as it has parsed the container
    // index. The previous implementation awaited `loadedmetadata` (up to a
    // hard-coded 15s) plus a fixed 150ms *before* touching the element, which
    // added that entire wait to every seek issued during page load.
    const appliedDirectly = applyMediaSeek(media, time);

    currentTime = time;
    dispatch('timeupdate', { currentTime: time, duration });

    if (media.readyState < HAVE_METADATA) {
      // Plyr's own `currentTime` setter bails while its duration is still 0, so
      // its progress bar would stay at zero. Re-apply through Plyr once metadata
      // lands — this only syncs the control UI, the seek itself already happened.
      const ready = await waitForMediaMetadata(media);
      if (requestId !== seekRequestId) return;
      if (!ready && !appliedDirectly) {
        // Metadata never arrived and the direct assignment failed; nothing more
        // we can usefully do without guessing.
        return;
      }
      if (!appliedDirectly) applyMediaSeek(media, time);
    }

    // Keep Plyr's internal clock in sync (progress bar, time readout) —
    // best-effort: `player` may still not exist yet if this resolved before
    // Plyr was constructed, in which case Plyr will simply pick up the
    // media element's already-seeked position when it initializes.
    if (player && Math.abs((player.currentTime ?? 0) - time) > 0.05) {
      player.currentTime = time;
    }
  }

  // Monotonic id of the latest caption fetch: a slow response must not overwrite a newer one.
  let captionsRequestId = 0;
  let captionsShownOriginal: boolean | null = null;

  /** Whether the viewer switched captions off in Plyr (its stored preference). */
  function captionsTurnedOffByUser(): boolean {
    try {
      return JSON.parse(localStorage.getItem('plyr') || '{}')?.captions === false;
    } catch {
      return false;
    }
  }

  /**
   * (Re)load the caption track from the server: on player ready, after a transcript or
   * speaker edit, and when the page toggles the redacted/original view.
   *
   * The cues come from `GET /files/{uuid}/captions` (see `$lib/api/captions`), cut into
   * broadcast-style cues there. When the server refuses (redaction scan still running,
   * policy unavailable) the track is emptied rather than left showing older cues.
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
        speakerPlaceholder: $t('speaker.localizedLabel', { number: '{number}' })
      });
    } catch (error) {
      console.warn('Captions are unavailable for this file:', error);
    }
    if (requestId !== captionsRequestId || player !== activePlayer) {
      return;
    }

    try {
      const videoElement = (activePlayer as any).media as HTMLVideoElement;
      trackElement = videoElement?.querySelector('track[kind="captions"]') as HTMLTrackElement;
      if (!trackElement) {
        return;
      }

      const vttUrl = URL.createObjectURL(new Blob([webvtt], { type: 'text/vtt' }));
      if (currentSubtitleBlobUrl) {
        URL.revokeObjectURL(currentSubtitleBlobUrl);
      }
      currentSubtitleBlobUrl = vttUrl;

      if (currentLoadHandler) {
        trackElement.removeEventListener('load', currentLoadHandler);
      }
      trackElement.src = vttUrl;

      const textTrack = trackElement.track;
      if (!textTrack) {
        return;
      }
      // 'hidden' loads the cues without native rendering; Plyr draws them itself and
      // shows them only while its CC toggle is on, so the viewer's choice is kept.
      textTrack.mode = 'hidden';
      currentLoadHandler = () => {
        textTrack.mode = 'hidden';
        const plyrCaptions = (activePlayer as any).captions;
        if (!captionsTurnedOffByUser() && plyrCaptions && !plyrCaptions.toggled) {
          activePlayer.toggleCaptions(true);
        }
      };
      trackElement.addEventListener('load', currentLoadHandler, { once: true });
    } catch (error) {
      console.error('Error updating subtitles:', error);
    }
  }

  $: if (player && captionsShownOriginal !== null && showOriginal !== captionsShownOriginal) {
    updateSubtitles();
  }

  function initializePlyr() {
    if (!mediaElement || player) {
      return;
    }

    const isAudio = playsAsAudio;
    const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

    // Build controls array based on media type and platform - YouTube style layout
    const controls = [
      'play-large',
      'play',
      'current-time', // Elapsed time
      'duration', // Total time
      'progress' // Progress bar will be repositioned via CSS
    ];

    // Add volume controls first, then CC to the right of volume
    controls.push('mute', 'volume');

    // Add captions only for video - to the right of volume
    if (!isAudio) {
      controls.push('captions');
    }

    // Add remaining controls
    controls.push('settings');
    if (!isAudio) {
      controls.push('pip');
    }
    if (isMac) {
      controls.push('airplay');
    }
    if (!isAudio) {
      controls.push('fullscreen');
    }

    // Settings menu options
    const settings = isAudio ? ['speed'] : ['captions', 'speed'];

    const plyrConfig = {
      controls,
      settings,
      iconUrl: '/plyr.svg',
      // Plyr's default points at cdn.plyr.io and is requested by the browser whenever a
      // player is destroyed or its source changes; serve it locally so no third party is hit.
      blankVideo: '/plyr-blank.mp4',
      keyboard: { global: true },
      tooltips: { controls: true },
      captions: {
        active: true,
        language: 'auto',
        update: true
      },
      playsinline: true,
      fullscreen: { iosNative: true }
    };

    try {
      // Initialize Plyr
      const newPlayer = new Plyr(mediaElement, plyrConfig);
      player = newPlayer;
      activeSrc = videoUrl;

      // Set up event listeners
      newPlayer.on('ready', () => {
        duration = newPlayer.duration;
        dispatch('loadedmetadata', { duration });

        // Load the caption track once Plyr has finished wiring its own captions UI.
        if (file?.status === 'completed') {
          setTimeout(() => {
            updateSubtitles();
          }, 500);
        }
      });

      newPlayer.on('timeupdate', () => {
        currentTime = newPlayer.currentTime;
        duration = newPlayer.duration;
        dispatch('timeupdate', {
          currentTime,
          duration
        });
      });

      newPlayer.on('play', () => dispatch('play'));
      newPlayer.on('pause', () => dispatch('pause'));
      newPlayer.on('ended', () => dispatch('ended'));
      newPlayer.on('seeking', () => {
        isSeeking = true;
        dispatch('seeking');
      });
      newPlayer.on('seeked', () => {
        if (seekSpinnerTimer !== null) {
          clearTimeout(seekSpinnerTimer);
          seekSpinnerTimer = null;
        }
        isSeeking = false;
        dispatch('seeked');
      });
      // A media error while seeking would otherwise leave the spinner up until
      // the fallback timer.
      newPlayer.on('error', () => {
        if (seekSpinnerTimer !== null) {
          clearTimeout(seekSpinnerTimer);
          seekSpinnerTimer = null;
        }
        isSeeking = false;
      });

    } catch (error) {
      console.error('Error creating Plyr player:', error);
    }
  }

  function destroyPlyr() {
    // Clean up subtitle blob URL
    if (currentSubtitleBlobUrl) {
      URL.revokeObjectURL(currentSubtitleBlobUrl);
      currentSubtitleBlobUrl = null;
    }

    // Clean up event listener reference
    if (currentLoadHandler && trackElement) {
      trackElement.removeEventListener('load', currentLoadHandler);
      currentLoadHandler = null;
    }

    if (player) {
      try {
        player.destroy();
        player = null;
      } catch (error) {
        console.error('Error destroying Plyr player:', error);
      }
    }
  }

  onMount(() => {
    if (videoUrl && mediaElement) {
      initializePlyr();
    }
  });

  onDestroy(() => {
    destroyPlyr();
  });

  // Watch for changes to videoUrl or mediaElement to initialize player
  $: if (videoUrl && mediaElement && !player) {
    initializePlyr();
  }

  // Seamless presigned-URL refresh: reload the new credential but resume exactly
  // where we left off. Changing the <source> src alone does not reload the element.
  $: if (player && mediaElement && videoUrl && videoUrl !== activeSrc) {
    hotSwapVideoSource(videoUrl);
  }

  async function hotSwapVideoSource(newUrl: string) {
    activeSrc = newUrl;
    if (!player || !mediaElement) return;
    const media = ((player as any).media as HTMLMediaElement | undefined) ?? mediaElement;
    const resumeAt = media.currentTime;
    const wasPlaying = !media.paused && !media.ended;
    const sourceEl = media.querySelector('source');
    if (sourceEl) sourceEl.src = newUrl;
    media.load();
    await seekToTime(resumeAt);
    if (wasPlaying) {
      const playResult = player.play();
      if (playResult && typeof playResult.catch === 'function') {
        playResult.catch(() => {});
      }
    }
  }
</script>

<div class="video-player-container">
  {#if videoUrl}
    {#if playbackMode === 'audio_only'}
      <p class="audio-only-notice" role="status">{$t('videoPlayer.audioOnlyPreview')}</p>
    {/if}
    {#if playsAsAudio}
      <!-- Plyr Audio Player -->
      <!-- svelte-ignore a11y-media-has-caption -->
      <audio
        bind:this={mediaElement}
        id="player"
        preload="metadata"
        playsinline
      >
        <source src={videoUrl} type={sourceType} />
        {$t('videoPlayer.audioNotSupported')}
      </audio>
    {:else}
      <!-- Plyr Video Player -->
      <!-- svelte-ignore a11y-media-has-caption -->
      <video
        bind:this={mediaElement}
        id="player"
        preload="metadata"
        playsinline
      >
        <source src={videoUrl} type={sourceType} />
        <!-- Always include track element so CC button appears -->
        <track kind="captions" label={$t('videoPlayer.captionsLabel')} srclang="en" default />
        {$t('videoPlayer.videoNotSupported')}
      </video>
    {/if}

    {#if isPlayerBuffering}
      <div class="buffer-indicator">
        <Spinner size="large" color="white" />
        <div class="buffer-text">{$t('videoPlayer.loadingProgress', { progress: Math.round(loadProgress) })}</div>
      </div>
    {/if}

    {#if isSeeking}
      <div class="seek-overlay">
        <Spinner size="medium" color="white" />
      </div>
    {/if}

    {#if errorMessage}
      <p class="error-message">{errorMessage}</p>
      <button
        class="retry-button"
        on:click={handleRetry}
        title={$t('videoPlayer.retryLoadingTitle')}
      >{$t('videoPlayer.retryLoading')}</button>
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
    border-radius: 8px;
    overflow: visible !important;
    background: var(--surface-color);
    z-index: 100;
  }

  /* Force container overflow and positioning for audio menus */
  :global(.plyr--audio) {
    overflow: visible !important;
    transform: none !important;
  }

  /* Ensure parent containers don't clip */
  :global(.video-column) {
    overflow: visible !important;
  }

  :global(.main-content-grid) {
    overflow: visible !important;
  }

  :global(.plyr) {
    border-radius: 8px;
    overflow: visible;
  }

  /* Round the video wrapper corners instead — visible overflow on .plyr
     allows the progress bar thumb to extend above the controls without clipping */
  :global(.plyr--video) {
    overflow: visible !important;
  }

  :global(.plyr__video-wrapper) {
    border-radius: 8px;
  }

  /* Uniform hover for all video controls — same translucent white */
  :global(.plyr--video .plyr__control:hover) {
    background: rgba(255, 255, 255, 0.25) !important;
  }

  /* All video control buttons — transparent default, white icon */
  :global(.plyr--video .plyr__control) {
    background: transparent !important;
    color: white !important;
  }

  /* Caption cues (server-built, at most two short lines each). The box sits over the
     video, so it is the same dark backdrop in light and dark themes; text wraps inside
     the frame and scales with the player. `unicode-bidi: plaintext` gives every line the
     direction of its own text, so an English cue stays LTR in the Arabic UI and an
     Arabic cue is RTL in any UI (the per-cue equivalent of dir="auto"). */
  :global(.plyr__captions) {
    padding-inline: 4%;
    font-size: clamp(14px, 2.6cqi, 28px);
    unicode-bidi: plaintext;
  }

  :global(.plyr--video) {
    container-type: inline-size;
  }

  :global(.plyr__caption) {
    display: inline;
    max-width: 100%;
    background: rgba(0, 0, 0, 0.78);
    color: #fff;
    line-height: 1.35;
    white-space: pre-line;
    overflow-wrap: anywhere;
    unicode-bidi: plaintext;
    -webkit-box-decoration-break: clone;
    box-decoration-break: clone;
  }

  /* The name is its own bidi island, so the LINE takes the direction of the speech, not of
     the name: an Arabic placeholder label must not turn an English line right-to-left. */
  :global(.plyr__caption .speaker) {
    font-weight: var(--font-weight-semibold, 600);
    color: #ffe58a;
    unicode-bidi: plaintext;
  }

  /* Captions (CC) button — override Plyr's pressed/active background */
  :global(.plyr--video .plyr__control[data-plyr="captions"]) {
    background: transparent !important;
  }

  :global(.plyr--video .plyr__control[data-plyr="captions"]:hover) {
    background: rgba(255, 255, 255, 0.25) !important;
  }

  :global(.plyr--video .plyr__control[data-plyr="captions"].plyr__control--pressed) {
    background: transparent !important;
  }

  :global(.plyr--video .plyr__control[data-plyr="captions"].plyr__control--pressed:hover) {
    background: rgba(255, 255, 255, 0.25) !important;
  }

  /* Settings gear inside .plyr__menu — ensure same hover as other controls */
  :global(.plyr--video .plyr__menu .plyr__control[aria-expanded]) {
    background: transparent !important;
  }

  :global(.plyr--video .plyr__menu .plyr__control[aria-expanded]:hover) {
    background: rgba(255, 255, 255, 0.25) !important;
  }

  /* Settings menu container - transparent background */
  :global(.plyr--video .plyr__controls__item.plyr__menu) {
    background: transparent !important;
    border: none !important;
  }

  :global(.plyr--video .plyr__volume input[type="range"]::-webkit-slider-thumb:hover) {
    background: #ffffff !important;
  }

  :global(.plyr--video .plyr__volume input[type="range"]::-moz-range-thumb:hover) {
    background: #ffffff !important;
  }

  /* YouTube-style progress bar positioning - above controls, full player width - VIDEO ONLY */
  :global(.plyr--video .plyr__controls) {
    position: absolute !important;
    bottom: 0 !important;
    left: 0 !important;
    right: 0 !important;
    padding-top: 12px !important; /* Increased spacing to prevent overlap */
    overflow: visible !important;
  }

  /* Position progress bar above controls at full player width - moved up - VIDEO ONLY */
  :global(.plyr--video .plyr__progress) {
    position: absolute !important;
    top: -8px !important; /* Moved up to prevent button overlap */
    left: 0 !important;
    right: 0 !important;
    width: 100% !important;
    height: auto !important;
    margin: 0 !important;
    padding: 0 !important;
    z-index: 10 !important;
    overflow: visible !important;
  }

  :global(.plyr--video .plyr__progress__container) {
    overflow: visible !important;
  }

  /* Reset Plyr's default negative margins and calc(100% + 13px) width on the
     range input and buffer that cause them to extend past the player edges. */
  :global(.plyr--video .plyr__progress input[type="range"]),
  :global(.plyr--video .plyr__progress__buffer) {
    width: 100% !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    padding: 0 !important;
  }

  /* Style the progress bar to be more prominent like YouTube - VIDEO ONLY */
  :global(.plyr--video .plyr__progress input[type="range"]::-webkit-slider-track) {
    height: 4px !important;
  }

  :global(.plyr--video .plyr__progress input[type="range"]::-moz-range-track) {
    height: 4px !important;
  }

  /* Make the progress bar slightly thicker on hover - VIDEO ONLY */
  :global(.plyr--video .plyr__progress:hover input[type="range"]::-webkit-slider-track) {
    height: 6px !important;
  }

  :global(.plyr--video .plyr__progress:hover input[type="range"]::-moz-range-track) {
    height: 6px !important;
  }

  /* Style time displays next to play button like YouTube - elapsed / total */
  :global(.plyr__time) {
    margin-left: 8px !important;
    margin-right: 2px !important;
    font-size: 14px !important;
    color: rgba(255, 255, 255, 0.9) !important;
  }

  /* Add slash separator between current time and duration */
  :global(.plyr__time--current-time::after) {
    content: " / " !important;
    color: rgba(255, 255, 255, 0.7) !important;
  }

  /* Style duration time */
  :global(.plyr__time--duration) {
    margin-left: 0 !important;
    margin-right: 16px !important;
  }

  /* Ensure proper spacing between controls */
  :global(.plyr__control) {
    margin-right: 4px !important;
  }

  /* Dark mode styles for audio players only */
  :global(.plyr--audio) {
    background: var(--surface-color) !important;
    color: var(--text-color) !important;
  }

  :global(.plyr--audio .plyr__controls) {
    background: var(--surface-color) !important;
    border-color: var(--border-color) !important;
    padding-top: 28px !important; /* Increased space above controls for progress bar visibility */
  }

  :global(.plyr--audio .plyr__control:not([data-plyr="speed"])) {
    color: var(--text-color) !important;
  }

  :global(.plyr--audio .plyr__control:not([data-plyr="speed"]):hover) {
    background: var(--hover-color) !important;
    color: var(--text-color) !important;
  }

  :global(.plyr--audio .plyr__control:not([data-plyr="speed"])[aria-pressed="true"]) {
    background: var(--primary-color) !important;
    color: white !important;
  }

  :global(.plyr--audio .plyr__time) {
    color: var(--text-color) !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]) {
    background: transparent !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-webkit-slider-track) {
    background: var(--slider-track) !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-moz-range-track) {
    background: var(--slider-track) !important;
  }

  /* Audio progress bar thumb - blue in light mode, white in dark mode */
  :global(.plyr--audio .plyr__progress input[type="range"]::-webkit-slider-thumb) {
    background: var(--slider-player-thumb) !important;
    border: none !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-moz-range-thumb) {
    background: var(--slider-player-thumb) !important;
    border: none !important;
  }

  /* Audio player progress bar positioning and sizing - AUDIO ONLY */
  :global(.plyr--audio .plyr__progress) {
    position: absolute !important;
    top: 6px !important; /* Position in the increased padding space with more clearance */
    left: 8px !important;
    right: 8px !important;
    width: calc(100% - 16px) !important;
    height: auto !important;
    margin: 0 !important;
    padding: 0 !important;
    z-index: 10 !important;
  }

  /* Make audio progress bar track more prominent */
  :global(.plyr--audio .plyr__progress input[type="range"]::-webkit-slider-track) {
    height: var(--slider-track-height) !important;
    border-radius: var(--slider-track-radius) !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-moz-range-track) {
    height: var(--slider-track-height) !important;
    border-radius: var(--slider-track-radius) !important;
  }

  /* Make audio progress bar thumb more prominent */
  :global(.plyr--audio .plyr__progress input[type="range"]::-webkit-slider-thumb) {
    width: var(--slider-thumb-size) !important;
    height: var(--slider-thumb-size) !important;
    border-radius: 50% !important;
    cursor: pointer !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-moz-range-thumb) {
    width: var(--slider-thumb-size) !important;
    height: var(--slider-thumb-size) !important;
    border-radius: 50% !important;
    cursor: pointer !important;
  }

  :global(.plyr--audio .plyr__volume input[type="range"]::-webkit-slider-track) {
    background: var(--slider-track) !important;
  }

  :global(.plyr--audio .plyr__volume input[type="range"]::-moz-range-track) {
    background: var(--slider-track) !important;
  }

  /* Audio volume thumb - blue in light mode, white in dark mode */
  :global(.plyr--audio .plyr__volume input[type="range"]::-webkit-slider-thumb) {
    background: var(--slider-player-thumb) !important;
    border: none !important;
  }

  :global(.plyr--audio .plyr__volume input[type="range"]::-moz-range-thumb) {
    background: var(--slider-player-thumb) !important;
    border: none !important;
  }

  /* Ensure audio player popups appear above other components */
  :global(.plyr--audio .plyr__menu) {
    z-index: 99999 !important;
    position: relative !important;
  }

  :global(.plyr--audio .plyr__menu__container) {
    z-index: 99999 !important;
    position: absolute !important;
    background: var(--surface-color) !important;
    color: var(--text-color) !important;
    border-radius: 0.5rem !important;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15) !important;
  }

  :global(.plyr--audio .plyr__menu__container::after) {
    border-top-color: var(--surface-color) !important;
  }

  :global(.plyr--audio .plyr__menu__container label),
  :global(.plyr--audio .plyr__menu__container span) {
    color: var(--text-color) !important;
  }

  :global(.plyr--audio .plyr__menu__container .plyr__control) {
    z-index: 99999 !important;
  }

  :global(.plyr--audio .plyr__tooltip) {
    z-index: 99999 !important;
  }

  /* When hovering a control, raise it above the progress bar (z-index: 10)
     so the tooltip is not hidden behind the progress bar */
  :global(.plyr--audio .plyr__control:hover) {
    z-index: 20 !important;
  }

  :global(.plyr--audio .plyr__volume) {
    z-index: 1000 !important;
  }

  /* Ensure the settings button and its dropdown have highest z-index */
  :global(.plyr--audio .plyr__control--overlaid) {
    z-index: 99998 !important;
  }

  :global(.plyr--audio .plyr__control[aria-haspopup="true"]) {
    z-index: 99998 !important;
  }

  /* Force the entire audio player container to have elevated z-index when menu is open */
  :global(.plyr--audio.plyr--menu-open) {
    z-index: 99997 !important;
    position: relative !important;
  }

  /* Dark mode styles for audio player settings popup menu only */
  :global(.plyr--audio .plyr__menu) {
    background: var(--surface-color) !important;
    border: none !important;
    color: var(--text-color) !important;
  }

  /* Target the menu container with role="menu" */
  :global(.plyr--audio [role="menu"]) {
    background: var(--surface-color) !important;
  }

  /* Target speed control buttons */
  :global(.plyr--audio button[data-plyr="speed"]) {
    color: var(--text-color) !important;
    background: transparent !important;
    border: none !important;
  }

  :global(.plyr--audio button[data-plyr="speed"]:hover) {
    background: var(--hover-color) !important;
    color: var(--text-color) !important;
  }

  /* Selected speed option */
  :global(.plyr--audio button[data-plyr="speed"][aria-checked="true"]) {
    background: var(--primary-color) !important;
    color: white !important;
  }

  /* Speed button spans */
  :global(.plyr--audio button[data-plyr="speed"] span) {
    color: inherit !important;
  }

  /* Dark mode styles for video player settings popup menu */
  :global(.plyr--video .plyr__menu) {
    background: var(--surface-color) !important;
    border: none !important;
    color: var(--text-color) !important;
  }

  /* Target the menu container with role="menu" for video */
  :global(.plyr--video [role="menu"]) {
    background: var(--surface-color) !important;
  }

  /* Menu popup container for video - override Plyr default white background */
  :global(.plyr--video .plyr__menu__container) {
    background: var(--surface-color) !important;
    color: var(--text-color) !important;
    border-radius: 0.5rem !important;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15) !important;
  }

  :global(.plyr--video .plyr__menu__container::after) {
    border-top-color: var(--surface-color) !important;
  }

  :global(.plyr--video .plyr__menu__container label),
  :global(.plyr--video .plyr__menu__container span) {
    color: var(--text-color) !important;
  }

  /* Force all menu items in video to have dark theme colors */
  :global(.plyr--video .plyr__menu .plyr__control) {
    color: var(--text-color) !important;
    background: transparent !important;
  }

  :global(.plyr--video .plyr__menu .plyr__control:hover) {
    background: var(--hover-color) !important;
    color: var(--text-color) !important;
  }

  /* Target speed and caption control buttons for video */
  :global(.plyr--video button[data-plyr="speed"]),
  :global(.plyr--video button[data-plyr="captions"]) {
    color: var(--text-color) !important;
    background: transparent !important;
    border: none !important;
  }

  :global(.plyr--video button[data-plyr="speed"]:hover),
  :global(.plyr--video button[data-plyr="captions"]:hover) {
    background: var(--hover-color) !important;
    color: var(--text-color) !important;
  }

  /* Selected options for video */
  :global(.plyr--video button[data-plyr="speed"][aria-checked="true"]),
  :global(.plyr--video button[data-plyr="captions"][aria-checked="true"]) {
    background: var(--primary-color) !important;
    color: white !important;
  }

  /* Button spans for video (exclude tooltips) */
  :global(.plyr--video button[data-plyr="speed"] span:not(.plyr__tooltip)),
  :global(.plyr--video button[data-plyr="captions"] span:not(.plyr__tooltip)) {
    color: inherit !important;
  }

  /* Video settings menu forward buttons */
  :global(.plyr--video button[data-plyr="settings"].plyr__control--forward) {
    color: var(--text-color) !important;
    background: transparent !important;
    border: none !important;
  }

  :global(.plyr--video button[data-plyr="settings"].plyr__control--forward:hover) {
    background: var(--hover-color) !important;
    color: var(--text-color) !important;
  }

  /* Language controls - ensure readable text */
  :global(.plyr--video button[data-plyr="language"]) {
    color: var(--text-color) !important;
    background: var(--surface-color) !important;
    border: none !important;
  }

  :global(.plyr--video button[data-plyr="language"]:hover) {
    background: var(--hover-color) !important;
    color: var(--text-color) !important;
  }

  /* Selected language option */
  :global(.plyr--video button[data-plyr="language"][aria-checked="true"]) {
    background: var(--primary-color) !important;
    color: white !important;
  }

  /* Menu value spans and badge */
  :global(.plyr--video .plyr__menu__value),
  :global(.plyr--video .plyr__badge) {
    color: inherit !important;
  }

  /* Dark mode for menu back button - audio only */
  :global(.plyr--audio button.plyr__control--back) {
    color: var(--text-color) !important;
    background: transparent !important;
    border: none !important;
  }

  :global(.plyr--audio button.plyr__control--back:hover) {
    background: var(--hover-color) !important;
    color: var(--text-color) !important;
  }

  :global(.plyr--audio button.plyr__control--back span) {
    color: inherit !important;
  }

  /* Dark mode for video menu back button */
  :global(.plyr--video button.plyr__control--back) {
    color: var(--text-color) !important;
    background: var(--surface-color) !important;
    border: none !important;
  }

  :global(.plyr--video button.plyr__control--back:hover) {
    background: var(--hover-color) !important;
    color: var(--text-color) !important;
  }

  :global(.plyr--video button.plyr__control--back span) {
    color: inherit !important;
  }

  .buffer-indicator {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    text-align: center;
    color: white;
    z-index: 10;
  }

  .buffer-text {
    font-size: 14px;
    font-weight: 500;
  }

  .seek-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10;
    pointer-events: none;
    border-radius: 8px;
  }

  .error-message {
    color: var(--error-color);
    margin: 10px 0;
    text-align: center;
  }

  .audio-only-notice {
    margin: 0 0 8px;
    padding: 8px 12px;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    background: var(--background-color);
    color: var(--text-secondary);
    font-size: 0.8125rem;
    text-align: center;
  }

  .retry-button {
    background: var(--primary-color);
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
    transition: background-color 0.2s;
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
    border-radius: 8px;
    color: var(--text-muted);
  }
</style>
