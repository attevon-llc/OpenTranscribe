<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import Plyr from 'plyr';
  import 'plyr/dist/plyr.css';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import { formatTimeWithMillis } from '$lib/utils/formatting';
  import Spinner from './ui/Spinner.svelte';

  export let videoUrl: string = '';
  export let file: any = null;
  export let isPlayerBuffering: boolean = false;
  export let loadProgress: number = 0;
  export let errorMessage: string = '';
  export let speakerList: any[] = [];

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


  function handleRetry() {
    dispatch('retry');
  }

  // External seek function for waveform and transcript clicks
  export async function seekToTime(time: number) {
    if (!player) {
      return;
    }

    // Show seeking spinner immediately
    isSeeking = true;

    const media = (player as any).media as HTMLMediaElement | undefined;

    // For large files (especially audio), the media may not have metadata loaded yet
    // when this is called from the ?t= parameter handler on page load.
    // Wait for loadedmetadata before attempting to seek.
    if (media && media.readyState < 1) {
      await new Promise<void>((resolve) => {
        const onReady = () => {
          media.removeEventListener('loadedmetadata', onReady);
          media.removeEventListener('canplay', onReady);
          resolve();
        };
        media.addEventListener('loadedmetadata', onReady, { once: true });
        media.addEventListener('canplay', onReady, { once: true });
        // Check if already ready (race condition with event)
        if (media.readyState >= 1) {
          media.removeEventListener('loadedmetadata', onReady);
          media.removeEventListener('canplay', onReady);
          resolve();
          return;
        }
        // Timeout for very large files (15s)
        setTimeout(() => {
          media.removeEventListener('loadedmetadata', onReady);
          media.removeEventListener('canplay', onReady);
          resolve();
        }, 15000);
      });
      // Brief delay to let Plyr process its own metadata/ready handlers
      // so its internal duration is valid (Plyr bails on seek if duration is 0)
      await new Promise(r => setTimeout(r, 150));
    }

    // Update player time via Plyr
    player.currentTime = time;
    // Also set directly on the media element as fallback — Plyr's setter
    // checks `if (!this.duration) return` which can bail if Plyr hasn't
    // finished initializing its internal state yet
    if (media) {
      media.currentTime = time;
    }
    currentTime = time;

    // Dispatch the seek event to parent
    dispatch('timeupdate', {
      currentTime: time,
      duration: duration
    });

    // Fallback: clear spinner if 'seeked' event never fires
    setTimeout(() => { isSeeking = false; }, 5000);
  }

  // External function to update subtitles when transcript becomes available or is edited
  export async function updateSubtitles() {
    if (!player || !file || !file.transcript_segments || !Array.isArray(file.transcript_segments) || file.transcript_segments.length === 0) {
      return;
    }
    const activePlayer = player;

    try {
      // Find the track element
      const videoElement = (activePlayer as any).media as HTMLVideoElement;
      trackElement = videoElement?.querySelector('track[kind="captions"]') as HTMLTrackElement;

      if (!trackElement) {
        // Track element might not be ready yet, silently return
        return;
      }

      // Create speaker display name mapping
      const speakerMapping = new Map();
      speakerList.forEach((speaker: any) => {
        speakerMapping.set(speaker.name, speaker.display_name || speaker.name);
      });

      // Generate WebVTT content from transcript segments with speaker mapping
      const webvttContent = generateWebVTTFromSegments(file.transcript_segments, speakerMapping);

      // Create blob URL for the WebVTT content
      const blob = new Blob([webvttContent], { type: 'text/vtt' });
      const vttUrl = URL.createObjectURL(blob);

      // Clean up any existing blob URL to prevent memory leaks
      if (currentSubtitleBlobUrl) {
        URL.revokeObjectURL(currentSubtitleBlobUrl);
      }
      currentSubtitleBlobUrl = vttUrl;

      // Remove any existing event listeners to prevent duplicates
      if (currentLoadHandler) {
        trackElement.removeEventListener('load', currentLoadHandler);
      }

      // Update the track element's src - this will trigger reload
      trackElement.src = vttUrl;

      // Set up proper track loading
      const textTrack = trackElement.track;
      if (textTrack) {
        // First disable the track
        textTrack.mode = 'disabled';

        // Create and store the load handler for proper cleanup
        currentLoadHandler = () => {
          textTrack.mode = 'showing';

          if ((activePlayer as any).captions) {
            (activePlayer as any).captions.active = true;
          }

          // Force Plyr to update its caption state
          if (activePlayer.elements?.buttons?.captions && !(activePlayer as any).captions?.active) {
            activePlayer.elements.buttons.captions.click();
          }
        };

        // Set up load event listener
        trackElement.addEventListener('load', currentLoadHandler, { once: true });

        // Also try setting up without waiting for load event (fallback)
        setTimeout(() => {
          if (textTrack.cues?.length === 0) {
            textTrack.mode = 'showing';

            if ((activePlayer as any).captions) {
              (activePlayer as any).captions.active = true;
            }
          }
        }, 1000);
      }

    } catch (error) {
      console.error('Error updating subtitles:', error);
    }
  }

  function initializePlyr() {
    if (!mediaElement || player) {
      return;
    }

    const isAudio = file?.content_type?.startsWith('audio/');
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

        // Initialize subtitles when player is ready
        if (file && file.status === 'completed' && file.transcript_segments && file.transcript_segments.length > 0) {
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
        isSeeking = false;
        dispatch('seeked');
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

  function generateWebVTTFromSegments(segments: any[], speakerMapping?: Map<string, string>): string {
    // Ensure proper WebVTT header with NOTE for debugging
    let webvtt = 'WEBVTT\n\nNOTE Generated from transcript segments\n\n';

    let validCues = 0;
    segments.forEach((segment, index) => {
      // Check for required fields with multiple possible field names
      const startTime = segment.start_time !== undefined ? segment.start_time : segment.start;
      const endTime = segment.end_time !== undefined ? segment.end_time : segment.end;
      const text = segment.text;

      if (startTime !== undefined && endTime !== undefined && text && startTime < endTime) {
        const formattedStartTime = formatTimeWithMillis(startTime);
        const formattedEndTime = formatTimeWithMillis(endTime);

        // Get original speaker name from segment
        const originalSpeakerName = segment.speaker_label || segment.speaker?.name || segment.speaker || `SPEAKER_${String(index + 1).padStart(2, '0')}`;

        // Use speaker mapping to get display name, fallback to translated speaker label
        const displaySpeakerName = speakerMapping?.get(originalSpeakerName) ||
                                  segment.speaker?.display_name ||
                                  translateSpeakerLabel(originalSpeakerName);

        const displayText = `${displaySpeakerName}: ${text.trim()}`;

        // Add cue with proper WebVTT format
        webvtt += `${formattedStartTime} --> ${formattedEndTime}\n`;
        webvtt += `${displayText}\n\n`;
        validCues++;
      }
    });

    return webvtt;
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
    {#if file?.content_type?.startsWith('audio/')}
      <!-- Plyr Audio Player -->
      <!-- svelte-ignore a11y-media-has-caption -->
      <audio
        bind:this={mediaElement}
        id="player"
        preload="auto"
        playsinline
      >
        <source src={videoUrl} type={file.content_type} />
        {$t('videoPlayer.audioNotSupported')}
      </audio>
    {:else}
      <!-- Plyr Video Player -->
      <!-- svelte-ignore a11y-media-has-caption -->
      <video
        bind:this={mediaElement}
        id="player"
        preload="auto"
        playsinline
      >
        <source src={videoUrl} type={file?.content_type || 'video/mp4'} />
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
    background: var(--border-color) !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-moz-range-track) {
    background: var(--border-color) !important;
  }

  /* Audio progress bar thumb - blue in light mode, white in dark mode */
  :global(.plyr--audio .plyr__progress input[type="range"]::-webkit-slider-thumb) {
    background: var(--primary-color) !important;
    border: none !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-moz-range-thumb) {
    background: var(--primary-color) !important;
    border: none !important;
  }

  /* Dark mode specific - white thumbs */
  :global([data-theme='dark'] .plyr--audio .plyr__progress input[type="range"]::-webkit-slider-thumb) {
    background: white !important;
  }

  :global([data-theme='dark'] .plyr--audio .plyr__progress input[type="range"]::-moz-range-thumb) {
    background: white !important;
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
    height: 6px !important;
    border-radius: 3px !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-moz-range-track) {
    height: 6px !important;
    border-radius: 3px !important;
  }

  /* Make audio progress bar thumb more prominent */
  :global(.plyr--audio .plyr__progress input[type="range"]::-webkit-slider-thumb) {
    width: 16px !important;
    height: 16px !important;
    border-radius: 50% !important;
    cursor: pointer !important;
  }

  :global(.plyr--audio .plyr__progress input[type="range"]::-moz-range-thumb) {
    width: 16px !important;
    height: 16px !important;
    border-radius: 50% !important;
    cursor: pointer !important;
  }

  :global(.plyr--audio .plyr__volume input[type="range"]::-webkit-slider-track) {
    background: var(--border-color) !important;
  }

  :global(.plyr--audio .plyr__volume input[type="range"]::-moz-range-track) {
    background: var(--border-color) !important;
  }

  /* Audio volume thumb - blue in light mode, white in dark mode */
  :global(.plyr--audio .plyr__volume input[type="range"]::-webkit-slider-thumb) {
    background: var(--primary-color) !important;
    border: none !important;
  }

  :global(.plyr--audio .plyr__volume input[type="range"]::-moz-range-thumb) {
    background: var(--primary-color) !important;
    border: none !important;
  }

  /* Dark mode specific - white volume thumbs */
  :global([data-theme='dark'] .plyr--audio .plyr__volume input[type="range"]::-webkit-slider-thumb) {
    background: white !important;
  }

  :global([data-theme='dark'] .plyr--audio .plyr__volume input[type="range"]::-moz-range-thumb) {
    background: white !important;
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

  .retry-button {
    background: #3b82f6;
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
