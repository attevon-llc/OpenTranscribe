<script lang="ts">
  /**
   * Inside the player element: the `<media-container>` (fullscreen target, hotkey and gesture
   * surface) with the host's slotted media, the caption overlay, the controls, the shortcuts,
   * the screen-reader announcer and the media error dialog
   * (https://videojs.org/docs/framework/html/guides/build-your-own-skin).
   */
  import { playerHotkeys } from '$lib/player/hotkeys';
  import CaptionOverlay from './CaptionOverlay.svelte';
  import MediaControls from './MediaControls.svelte';

  export let audio = false;
  export let variant: 'full' | 'mini' = 'full';
  export let media: HTMLMediaElement | null = null;

  // The file page owns its page, so its shortcuts are page-wide (`target="document"`);
  // the mini player's stay inside it. An omitted `value` is the element's own default.
  $: hotkeys = playerHotkeys(variant === 'full' ? 'document' : 'player');
</script>

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
      value={hotkey.value}
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
