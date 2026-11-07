<script lang="ts">
  /**
   * The composed Video.js 10 player around a host's own `<video>`/`<audio>` (plan #1327).
   *
   * Markup only: the host renders its media element in the default slot, builds the
   * `PlayerHandle` with `$lib/player/videojsPlayer` and listens to native media events.
   * Structure, per https://videojs.org/docs/framework/html/guides/build-your-own-skin:
   * `<media-i18n>` (labels follow `<html lang>`/`dir`) > `<video-player>` or `<audio-player>`
   * (the store, one preset each) > `PlayerSurface` (the `<media-container>` and everything in
   * it).
   */
  import { onDestroy } from 'svelte';
  import { locale } from '$stores/locale';
  import { bindPlayerLocale } from '$lib/player/i18n';
  import './elements';
  import './player.css';
  import PlayerSurface from './PlayerSurface.svelte';

  /** `<audio>`: the audio player and its controls (no captions, PiP or fullscreen). */
  export let audio = false;
  export let variant: 'full' | 'mini' = 'full';
  /** The slotted media element, for the caption overlay. */
  export let media: HTMLMediaElement | null = null;

  /**
   * The speed list, kept (plan row 20). Video.js offers 0.5 to 2 unless the player is given
   * its own list (https://videojs.org/docs/framework/html/reference/api/feature-playback-rate).
   */
  const PLAYBACK_RATES = '0.5 0.75 1 1.25 1.5 1.75 2 4';

  onDestroy(bindPlayerLocale(locale));
</script>

<media-i18n class="media-player" data-variant={variant}>
  {#if audio}
    <audio-player default-playback-rates={PLAYBACK_RATES}>
      <PlayerSurface audio {variant} {media}><slot /></PlayerSurface>
    </audio-player>
  {:else}
    <video-player default-playback-rates={PLAYBACK_RATES}>
      <PlayerSurface {variant} {media}><slot /></PlayerSurface>
    </video-player>
  {/if}
</media-i18n>
