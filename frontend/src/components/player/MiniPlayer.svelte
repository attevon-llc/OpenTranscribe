<script module lang="ts">
  let instanceCount = 0;

  /** Each mounted player's own id prefix, for the tooltip `trigger` and menu `commandfor` links. */
  function nextInstanceId(): string {
    instanceCount += 1;
    return `mini-player-${instanceCount}`;
  }
</script>

<script lang="ts">
  /**
   * The mini player: the floating search and speaker preview (issue #1327, plan Lane D).
   *
   * Video.js 10 controls composed in our own markup and tokens, no packaged skin
   * (https://videojs.org/docs/framework/html/guides/build-your-own-skin). The app reaches it
   * only through `getPlayer()` (a `PlayerHandle`) and the native media events it re-dispatches.
   * Clip start/end, autoplay at an offset and the presigned-URL hot swap are app behaviour and
   * stay here; the library supplies the controls, their translations, RTL and announcements.
   */
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import type { createPlayer } from '@videojs/html';
  import type { videoFeatures } from '@videojs/html/video';
  import type { audioFeatures } from '@videojs/html/audio';
  import '$components/player/miniPlayerElements';
  import WaveformPlayer from '$components/WaveformPlayer.svelte';
  import { locale } from '$stores/locale';
  import { applyMediaSeek, waitForMediaMetadata, HAVE_METADATA } from '$lib/utils/mediaReady';
  import { createVideojsHandle } from '$lib/player/videojsHandle';
  import { bindPlayerPrefs } from '$lib/player/prefs';
  import { bindPlayerLocale } from '$lib/player/i18n';
  import { attachLoopHotkey, playerHotkeys } from '$lib/player/hotkeys';
  import { attachFullWindowKeys, supportsElementFullscreen } from '$lib/player/fullWindow';
  import { listenToMedia, mediaDuration } from '$lib/player/mediaEvents';
  import type { PlayerHandle } from '$lib/player/types';

  // Derived through `createPlayer`: `VideoPlayerElement['store']` is `any` in 10.3.0
  // (lib/player/CLAUDE.md, "Video.js 10 rules").
  type VideoPlayerElement = InstanceType<
    ReturnType<typeof createPlayer<typeof videoFeatures>>['PlayerElement']
  >;
  type AudioPlayerElement = InstanceType<
    ReturnType<typeof createPlayer<typeof audioFeatures>>['PlayerElement']
  >;

  export let mediaUrl: string;
  export let contentType: string;
  export let startTime: number = 0;
  export let endTime: number = 0;
  export let autoplay: boolean = true;
  export let fileId: string = '';
  export let compact: boolean = true;

  /** The speed list, 4x included (plan row 20); the library's default list stops at 2. */
  const PLAYBACK_RATES = '0.5 0.75 1 1.25 1.5 1.75 2 4';

  const dispatch = createEventDispatcher();
  const id = nextInstanceId();

  let playerElement: VideoPlayerElement | AudioPlayerElement;
  let containerElement: HTMLElement;
  let mediaElement: HTMLVideoElement | HTMLAudioElement;
  let handle: PlayerHandle | null = null;
  let cleanups: (() => void)[] = [];
  let seeking = true;
  let playerCurrentTime = 0;
  let playerDuration = 0;
  let fallbackTimeout: ReturnType<typeof setTimeout>;
  // The URL currently loaded into the media element, so a presigned-URL refresh (a new
  // `mediaUrl`) can swap the credential without losing the position (hotSwapSource).
  let activeSrc = '';
  // Decided on mount: `document` does not exist while a route renders on the server.
  let elementFullscreen = true;
  let fullWindow = false;
  let bodyOverflow = '';

  $: isVideo = contentType?.startsWith('video/');
  // Container-scoped, so the panel never takes keys from the page under it. The mini player
  // has no captions or picture-in-picture control (the previous player had neither), and where the browser
  // cannot put an element fullscreen `F` is the full-window toggle instead.
  $: hotkeys = playerHotkeys('player')
    .filter(({ action }) => {
      if (action === 'toggleSubtitles' || action === 'togglePictureInPicture') return false;
      if (action === 'toggleFullscreen') return isVideo && elementFullscreen;
      return true;
    })
    .map(({ value, ...rest }) => (value === undefined ? rest : { ...rest, value: String(value) }));

  // A new presigned URL after the player is up: reload it, resume where we were.
  $: if (handle && mediaElement && mediaUrl && mediaUrl !== activeSrc) {
    void hotSwapSource(mediaUrl);
  }

  function initPlayer() {
    if (!mediaElement || !playerElement) {
      seeking = false;
      return;
    }
    const media = mediaElement;
    media.load();
    activeSrc = mediaUrl;

    const player = createVideojsHandle(media, playerElement.store);
    handle = player;

    // The first seek is issued at once, not gated on `canplay` (#649): assigning
    // `currentTime` before metadata is kept by the browser as the start position.
    void applySeek(startTime, { play: autoplay });

    // Persistent handlers, so a later hot swap's seek still clears the spinner (#649).
    cleanups.push(
      listenToMedia(media, {
        timeupdate: () => {
          playerCurrentTime = media.currentTime;
          playerDuration = mediaDuration(media);
          dispatch('timeupdate', { currentTime: media.currentTime });
          if (endTime > 0 && media.currentTime >= endTime) player.pause();
        },
        playing: () => {
          seeking = false;
          dispatch('play');
        },
        seeked: () => {
          seeking = false;
        },
        pause: () => dispatch('pause'),
        error: () => {
          seeking = false;
        },
        ended: () => {
          dispatch('pause');
          dispatch('ended');
        },
      }),
      bindPlayerPrefs(playerElement.store, media),
      bindPlayerLocale(locale),
      attachLoopHotkey(containerElement, media)
    );
    if (isVideo && !elementFullscreen) {
      cleanups.push(
        attachFullWindowKeys(containerElement, { isOn: () => fullWindow, set: setFullWindow })
      );
    }

    fallbackTimeout = setTimeout(() => {
      seeking = false;
    }, 15000);
  }

  /**
   * Seek, then (optionally) play once the element can. The seek goes to the element first,
   * without waiting for metadata (#645/#647); only starting playback waits. There is no
   * second clock to resync: the controls read the element (plan row 8).
   */
  async function applySeek(time: number, { play }: { play: boolean }) {
    if (!handle || !mediaElement) return;
    const media = mediaElement;
    const player = handle;

    const appliedDirectly = time > 0 ? applyMediaSeek(media, time) : true;
    // Paused first, so a slow seek never plays the start of the file while the target loads.
    if (time > 0) media.pause();

    if (media.readyState < HAVE_METADATA) {
      const ready = await waitForMediaMetadata(media);
      if (handle !== player) return;
      if (!ready && !appliedDirectly) {
        seeking = false;
        return;
      }
    }
    if (time > 0 && !appliedDirectly) player.seek(time);

    if (play) {
      player.play().catch(() => {
        seeking = false;
      });
    } else {
      seeking = false;
    }
  }

  async function hotSwapSource(newUrl: string) {
    activeSrc = newUrl;
    if (!handle || !mediaElement) return;
    const media = mediaElement;
    const resumeAt = media.currentTime;
    const wasPlaying = !media.paused && !media.ended;
    seeking = true;
    const sourceEl = media.querySelector('source');
    if (sourceEl) sourceEl.src = newUrl;
    media.load();
    await applySeek(resumeAt, { play: wasPlaying });
  }

  function setFullWindow(on: boolean) {
    if (on === fullWindow) return;
    // The previous fallback locked the page behind the player the same way.
    if (on) {
      bodyOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = bodyOverflow;
    }
    fullWindow = on;
  }

  export function seek(time: number) {
    void applySeek(time, { play: false });
  }

  export function getPlayer(): PlayerHandle | null {
    return handle;
  }

  function handleWaveformSeek(event: CustomEvent<{ time: number }>) {
    if (handle && event.detail?.time != null) handle.seek(event.detail.time);
  }

  onMount(() => {
    elementFullscreen = supportsElementFullscreen();
    initPlayer();
  });

  onDestroy(() => {
    clearTimeout(fallbackTimeout);
    cleanups.forEach((stop) => stop());
    cleanups = [];
    handle = null;
    if (fullWindow) setFullWindow(false);
  });
</script>

<div
  class="mini-player"
  class:compact
  class:audio-mode={!isVideo}
  class:video-mode={isVideo}
  class:full-window={fullWindow}
  data-full-window={fullWindow ? '' : undefined}
>
  {#if seeking}
    <div class="seek-overlay">
      <div class="seek-spinner"></div>
    </div>
  {/if}

  <!-- No `lang` on the provider: it follows <html lang>/dir, which $stores/locale sets. -->
  <media-i18n>
    <svelte:element
      this={isVideo ? 'video-player' : 'audio-player'}
      bind:this={playerElement}
      default-playback-rates={PLAYBACK_RATES}
    >
      <media-container class="mini-surface" bind:this={containerElement}>
        {#if isVideo}
          <!-- playsinline: iOS must not force its own fullscreen on play -->
          <video bind:this={mediaElement} preload="metadata" playsinline>
            <source src={mediaUrl} />
          </video>
          <media-gesture type="tap" action="togglePaused" pointer="mouse"></media-gesture>
          <media-gesture type="tap" action="toggleControls" pointer="touch"></media-gesture>
        {:else}
          <audio bind:this={mediaElement} preload="metadata">
            <source src={mediaUrl} />
          </audio>
        {/if}

        <media-controls visibility={isVideo ? 'auto' : 'always'}>
          <media-controls-content class="mini-controls">
            <media-tooltip-group>
              <media-time-slider class="mini-timeline">
                <media-slider-track class="mini-track">
                  <media-slider-buffer class="mini-buffer"></media-slider-buffer>
                  <media-slider-fill class="mini-fill"></media-slider-fill>
                </media-slider-track>
                <media-slider-thumb class="mini-thumb"></media-slider-thumb>
              </media-time-slider>

              <media-controls-group class="mini-row">
                <media-play-button class="mini-button" id="{id}-play">
                  <media-icon name="restart" class="icon-restart" aria-hidden="true"></media-icon>
                  <media-icon name="play" class="icon-play" aria-hidden="true"></media-icon>
                  <media-icon name="pause" class="icon-pause" aria-hidden="true"></media-icon>
                </media-play-button>
                <media-tooltip trigger="{id}-play" side="top" class="mini-tooltip">
                  <media-tooltip-label></media-tooltip-label>
                </media-tooltip>

                <media-time-group class="mini-time">
                  <media-time type="current"></media-time>
                  <media-time-separator></media-time-separator>
                  <media-time type="duration"></media-time>
                </media-time-group>

                <span class="mini-spacer"></span>

                <media-mute-button class="mini-button" id="{id}-mute">
                  <media-icon name="volume-off" class="icon-volume-off" aria-hidden="true"
                  ></media-icon>
                  <media-icon name="volume-low" class="icon-volume-low" aria-hidden="true"
                  ></media-icon>
                  <media-icon name="volume-high" class="icon-volume-high" aria-hidden="true"
                  ></media-icon>
                </media-mute-button>
                <media-tooltip trigger="{id}-mute" side="top" class="mini-tooltip">
                  <media-tooltip-label></media-tooltip-label>
                </media-tooltip>

                {#if !isVideo}
                  <!-- The mini player has a volume slider for audio only. -->
                  <media-volume-slider class="mini-volume">
                    <media-slider-track class="mini-track">
                      <media-slider-fill class="mini-fill"></media-slider-fill>
                    </media-slider-track>
                    <media-slider-thumb class="mini-thumb"></media-slider-thumb>
                  </media-volume-slider>
                {/if}

                <media-playback-rate-button
                  class="mini-button mini-rate"
                  id="{id}-rate"
                  commandfor="{id}-rates"
                >
                  <span data-part="value" dir="ltr"></span>
                </media-playback-rate-button>
                <media-tooltip trigger="{id}-rate" side="top" class="mini-tooltip">
                  <media-text token="menu.speed">Speed</media-text>
                </media-tooltip>
                <media-menu id="{id}-rates" side="top" align="end" class="mini-menu">
                  <media-menu-content>
                    <media-playback-rate-radio-group>
                      <template>
                        <media-menu-radio-item class="mini-menu-item">
                          <span data-part="label" dir="ltr"></span>
                          <media-menu-item-indicator force-mount class="mini-menu-check">
                            <media-icon name="check" aria-hidden="true"></media-icon>
                          </media-menu-item-indicator>
                        </media-menu-radio-item>
                      </template>
                    </media-playback-rate-radio-group>
                  </media-menu-content>
                </media-menu>

                {#if isVideo && elementFullscreen}
                  <media-fullscreen-button class="mini-button" id="{id}-fullscreen">
                    <media-icon name="fullscreen-enter" class="icon-fs-enter" aria-hidden="true"
                    ></media-icon>
                    <media-icon name="fullscreen-exit" class="icon-fs-exit" aria-hidden="true"
                    ></media-icon>
                  </media-fullscreen-button>
                  <media-tooltip trigger="{id}-fullscreen" side="top" class="mini-tooltip">
                    <media-tooltip-label></media-tooltip-label>
                  </media-tooltip>
                {:else if isVideo}
                  <!-- No element Fullscreen API: fill the window instead (lib/player/fullWindow). -->
                  <button
                    type="button"
                    class="mini-button"
                    aria-pressed={fullWindow}
                    on:click={() => setFullWindow(!fullWindow)}
                  >
                    {#if fullWindow}
                      <media-icon name="fullscreen-exit" aria-hidden="true"></media-icon>
                      <media-text class="visually-hidden" token="fullscreen.exit"
                        >Exit fullscreen</media-text
                      >
                    {:else}
                      <media-icon name="fullscreen-enter" aria-hidden="true"></media-icon>
                      <media-text class="visually-hidden" token="fullscreen.enter"
                        >Enter fullscreen</media-text
                      >
                    {/if}
                  </button>
                {/if}
              </media-controls-group>
            </media-tooltip-group>
          </media-controls-content>
        </media-controls>

        {#each hotkeys as hotkey (hotkey.keys)}
          <media-hotkey {...hotkey}></media-hotkey>
        {/each}
        <media-status-announcer class="visually-hidden"></media-status-announcer>
      </media-container>
    </svelte:element>
  </media-i18n>

  {#if !isVideo && fileId}
    <div class="mini-waveform">
      <WaveformPlayer
        {fileId}
        duration={playerDuration}
        currentTime={playerCurrentTime}
        height={60}
        on:seek={handleWaveformSeek}
      />
    </div>
  {/if}
</div>

<style>
  /* Video.js state lives in data-* attributes Svelte cannot see, and the parts it renders are
     custom elements, so their rules are :global() under this component's root
     (https://videojs.org/docs/framework/html/guides/svelte, "Keep runtime styles global"). */
  .mini-player {
    position: relative;
    display: flex;
    flex-direction: column;
    color: var(--text-color);
  }

  .mini-player :global(:is(media-i18n, video-player, audio-player, media-controls, media-tooltip-group)) {
    display: contents;
  }

  .mini-player :global(media-container) {
    position: relative;
    display: block;
  }

  .mini-player :global(:is(media-container, media-controls-content):focus-visible),
  .mini-player :global(.mini-button:focus-visible) {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: -2px;
  }

  .video-mode :global(video) {
    display: block;
    inline-size: 100%;
    max-block-size: 280px;
    background: var(--media-letterbox);
  }

  /* Seeking overlay (app-owned, plan row 9) */
  .seek-overlay {
    position: absolute;
    inset: 0;
    background: var(--modal-backdrop);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: var(--z-sticky);
    pointer-events: none;
  }

  .seek-spinner {
    inline-size: 28px;
    block-size: 28px;
    border: 3px solid color-mix(in srgb, var(--text-on-solid) 20%, transparent);
    border-inline-start-color: var(--text-on-solid);
    border-radius: var(--radius-round);
    animation: mini-spin 0.8s linear infinite;
  }

  @keyframes mini-spin {
    to {
      transform: rotate(360deg);
    }
  }

  /* Controls: over the picture for video, a bar on the surface for audio */
  .mini-player :global(.mini-controls) {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: var(--space-1-5) var(--space-1-5) var(--space-1);
    transition: opacity var(--duration-base) var(--ease-standard);
  }

  .video-mode :global(.mini-controls) {
    position: absolute;
    inset-inline: 0;
    inset-block-end: 0;
    color: var(--text-on-solid);
    background: linear-gradient(to top, var(--modal-backdrop), transparent);
  }

  /* Hidden while idle, unless a control inside has keyboard focus. */
  .video-mode :global(.mini-controls:not([data-visible]):not(:has(:focus-visible))) {
    opacity: 0;
  }

  .audio-mode :global(.mini-controls) {
    background: var(--surface-color);
    border-block-start: 1px solid var(--border-color);
  }

  .mini-player :global(.mini-row) {
    display: flex;
    align-items: center;
    gap: var(--space-0-5);
    flex-wrap: nowrap;
  }

  /* The control bar keeps its order in RTL, exactly like the file page's player (player.css
     `.controls-row:dir(rtl)`); only menus and chevrons mirror. */
  .mini-player :global(.mini-row:dir(rtl)) {
    flex-direction: row-reverse;
  }

  .mini-spacer {
    flex: 1;
  }

  .mini-player :global(.mini-button) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-inline-size: 28px;
    block-size: 28px;
    padding: var(--space-1);
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    font-size: var(--font-size-xs);
  }

  .compact :global(.mini-button) {
    min-inline-size: 26px;
    block-size: 26px;
  }

  .mini-player :global(.mini-button[hidden]) {
    display: none;
  }

  .video-mode :global(.mini-button:hover) {
    background: color-mix(in srgb, var(--text-on-solid) 25%, transparent);
  }

  .audio-mode :global(.mini-button:hover) {
    background: var(--hover-color);
  }

  .mini-player :global(media-icon) {
    display: inline-flex;
    inline-size: var(--icon-size-md);
    block-size: var(--icon-size-md);
  }

  .mini-player :global(media-icon > svg) {
    inline-size: 100%;
    block-size: 100%;
  }

  /* One icon per state (https://videojs.org/docs/framework/html/reference/components/play-button) */
  .mini-player :global(:is(.icon-restart, .icon-play, .icon-pause)),
  .mini-player :global(:is(.icon-volume-off, .icon-volume-low, .icon-volume-high)),
  .mini-player :global(:is(.icon-fs-enter, .icon-fs-exit)) {
    display: none;
  }

  .mini-player :global(media-play-button[data-ended] .icon-restart),
  .mini-player :global(media-play-button:not([data-ended])[data-paused] .icon-play),
  .mini-player :global(media-play-button:not([data-ended]):not([data-paused]) .icon-pause),
  .mini-player :global(media-mute-button[data-volume-level='off'] .icon-volume-off),
  .mini-player :global(media-mute-button[data-volume-level='low'] .icon-volume-low),
  .mini-player :global(media-mute-button[data-volume-level='medium'] .icon-volume-high),
  .mini-player :global(media-mute-button[data-volume-level='high'] .icon-volume-high),
  .mini-player :global(media-fullscreen-button:not([data-fullscreen]) .icon-fs-enter),
  .mini-player :global(media-fullscreen-button[data-fullscreen] .icon-fs-exit) {
    display: inline-flex;
  }

  /* A timestamp reads "elapsed / total" left to right in every locale (as in player.css). The
     separator's "/" comes from the library; adding one in CSS showed "//". */
  .mini-player :global(.mini-time) {
    direction: ltr;
    unicode-bidi: isolate;
    display: inline-flex;
    gap: var(--space-1);
    padding-inline: var(--space-1);
    font-size: var(--font-size-xs);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .mini-player :global(.mini-time media-time-separator) {
    opacity: 0.6;
  }

  /* Sliders: the value scale stays physical left-to-right in every language, by design
     (https://videojs.org/docs/framework/html/guides/internationalization, "Set text direction"),
     so the inline axis is pinned to LTR and the parts are placed on it. */
  .mini-player :global(:is(.mini-timeline, .mini-volume)) {
    --media-slider-fill: 0%;
    --media-slider-buffer: 0%;
    position: relative;
    display: flex;
    align-items: center;
    block-size: 14px;
    direction: ltr;
    cursor: pointer;
    touch-action: none;
  }

  .mini-player :global(.mini-timeline) {
    inline-size: 100%;
  }

  .mini-player :global(.mini-volume) {
    inline-size: 64px;
    flex-shrink: 0;
  }

  .mini-player :global(.mini-track) {
    position: relative;
    inline-size: 100%;
    block-size: var(--slider-track-height);
    border-radius: var(--slider-track-radius);
    background: color-mix(in srgb, currentcolor 30%, transparent);
    overflow: hidden;
  }

  .video-mode :global(.mini-timeline .mini-track) {
    block-size: 3px;
  }

  .video-mode :global(.mini-timeline:hover .mini-track) {
    block-size: 5px;
  }

  .mini-player :global(:is(.mini-buffer, .mini-fill)) {
    position: absolute;
    inset-block: 0;
    inset-inline-start: 0;
  }

  .mini-player :global(.mini-buffer) {
    inline-size: var(--media-slider-buffer);
    background: color-mix(in srgb, currentcolor 30%, transparent);
  }

  .mini-player :global(.mini-fill) {
    inline-size: var(--media-slider-fill);
    background: var(--slider-accent);
  }

  .mini-player :global(.mini-thumb) {
    position: absolute;
    inset-inline-start: var(--media-slider-fill);
    inline-size: var(--slider-thumb-size);
    block-size: var(--slider-thumb-size);
    border-radius: var(--radius-round);
    background: var(--slider-player-thumb);
    translate: -50% 0;
  }

  /* Speed menu and tooltips: popups in the top layer, on the app surface */
  .mini-player :global(.mini-menu) {
    min-inline-size: 96px;
    margin: 0;
    padding: var(--space-1);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--surface-color);
    color: var(--text-color);
    box-shadow: var(--dropdown-shadow);
    font-size: var(--font-size-sm);
  }

  .mini-player :global(.mini-menu:not([data-open])) {
    display: none;
  }

  .mini-player :global(.mini-menu-item) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .mini-player :global(.mini-menu-item[data-highlighted]) {
    background: var(--hover-color);
  }

  .mini-player :global(.mini-menu-item[aria-checked='true']) {
    color: var(--primary-on-surface);
  }

  .mini-player :global(.mini-menu-check) {
    opacity: 0;
  }

  .mini-player :global(.mini-menu-item[aria-checked='true'] .mini-menu-check) {
    opacity: 1;
  }

  .mini-player :global(.mini-tooltip) {
    margin: 0;
    padding: var(--space-0-5) var(--space-2);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--text-color);
    color: var(--surface-color);
    font-size: var(--font-size-xs);
    white-space: nowrap;
    pointer-events: none;
  }

  .mini-player :global(.mini-tooltip:not([data-open])) {
    display: none;
  }

  .mini-player :global(.visually-hidden) {
    position: absolute;
    inline-size: 1px;
    block-size: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    white-space: nowrap;
    border: 0;
    clip-path: inset(50%);
  }

  .mini-waveform {
    inline-size: 100%;
    padding: var(--space-2) var(--space-2) 0;
    background: var(--surface-color);
  }

  /* Full-window fallback (no element Fullscreen API): the player fills the viewport. */
  .mini-player.full-window {
    position: fixed;
    inset: 0;
    z-index: var(--z-max);
    justify-content: center;
    background: var(--media-letterbox);
  }

  .full-window :global(video) {
    max-block-size: none;
    block-size: 100vh;
    object-fit: contain;
  }

  .mini-player :global(media-container:fullscreen video) {
    max-block-size: none;
    block-size: 100%;
    object-fit: contain;
  }
</style>
