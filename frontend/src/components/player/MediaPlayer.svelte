<script lang="ts">
  /**
   * The composed Video.js 10 player around a host's own `<video>`/`<audio>` (plan #1327).
   *
   * Markup only: the host renders its media element in the default slot, builds the
   * `PlayerHandle` with `$lib/player/videojsPlayer` and listens to native media events.
   * Structure, per https://videojs.org/docs/framework/html/guides/build-your-own-skin:
   * `<media-i18n>` (labels follow `<html lang>`/`dir`) > `<video-player>`/`<audio-player>`
   * (the store) > `<media-container>` (fullscreen target, hotkey and gesture surface) > the
   * media, the caption overlay, the controls, the shortcuts, the screen-reader announcer and
   * the media error dialog.
   */
  import { onDestroy } from 'svelte';
  import { locale } from '$stores/locale';
  import { bindPlayerLocale } from '$lib/player/i18n';
  import { playerHotkeys } from '$lib/player/hotkeys';
  import './elements';
  import './player.css';
  import CaptionOverlay from './CaptionOverlay.svelte';
  import MediaControls from './MediaControls.svelte';

  /** `<audio>`: the audio player and its controls (no captions, PiP or fullscreen). */
  export let audio = false;
  export let variant: 'full' | 'mini' = 'full';
  /** The slotted media element, for the caption overlay. */
  export let media: HTMLMediaElement | null = null;

  /**
   * Plyr's speed list, kept (plan row 20). Video.js offers 0.5 to 2 unless the player is given
   * its own list (https://videojs.org/docs/framework/html/reference/api/feature-playback-rate).
   */
  const PLAYBACK_RATES = '0.5 0.75 1 1.25 1.5 1.75 2 4';

  // The file page owns its page, so its shortcuts are page-wide (Plyr's `keyboard.global`);
  // the mini player's stay inside it.
  $: hotkeys = playerHotkeys(variant === 'full' ? 'document' : 'player');

  onDestroy(bindPlayerLocale(locale));
</script>

<media-i18n class="media-player" data-variant={variant}>
  <svelte:element this={audio ? 'audio-player' : 'video-player'} default-playback-rates={PLAYBACK_RATES}>
    <media-container class="media-surface" class:audio>
      <slot />
      {#if !audio}
        <CaptionOverlay {media} />
      {/if}
      <MediaControls {audio} {variant} />

      {#each hotkeys as hotkey (hotkey.keys)}
        <media-hotkey
          keys={hotkey.keys}
          action={hotkey.action}
          target={hotkey.target}
          {...hotkey.value === undefined ? {} : { value: hotkey.value }}
        ></media-hotkey>
      {/each}
      {#if !audio}
        <media-gesture type="tap" action="togglePaused" pointer="mouse" region="center"></media-gesture>
        <media-gesture type="tap" action="toggleControls" pointer="touch"></media-gesture>
        <media-gesture type="doubletap" action="seekStep" region="left"></media-gesture>
        <media-gesture type="doubletap" action="toggleFullscreen" region="center"></media-gesture>
        <media-gesture type="doubletap" action="seekStep" region="right"></media-gesture>
      {/if}

      <media-status-announcer class="status-announcer"></media-status-announcer>

      <media-error-dialog>
        <media-dialog-popup class="error-dialog">
          <media-dialog-title class="error-title"></media-dialog-title>
          <media-dialog-description></media-dialog-description>
          <media-dialog-close class="error-close"></media-dialog-close>
        </media-dialog-popup>
      </media-error-dialog>
    </media-container>
  </svelte:element>
</media-i18n>
