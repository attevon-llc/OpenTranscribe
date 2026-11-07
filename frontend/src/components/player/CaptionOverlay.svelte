<script lang="ts">
  /**
   * Draws the showing caption track's active cues over the video (plan #1327, row 24).
   *
   * Video.js leaves cue rendering to the browser, styled with `::cue`
   * (https://videojs.org/docs/framework/html/guides/captions). That cannot carry this app's
   * captions: `::cue` forbids padding, wrapping and box properties, and `::cue(.speaker)` is not
   * supported in Firefox (MDN `::cue`, https://bugzil.la/1321489), so the speaker name would
   * lose its colour there. The browser's own painting is hidden (`video::cue { visibility:
   * hidden }`, a permitted `::cue` property everywhere) and the cues are drawn here from
   * `getCueAsHTML()`, which turns the server's `<c.speaker>` into `<span class="speaker">` as a
   * DOM fragment (never parsed as markup).
   *
   * Follows the track the player shows, so the CC button, the `c` key and the remembered
   * choice all apply with no extra state here.
   */
  import { onDestroy } from 'svelte';
  import { captionTracks } from '$lib/player/videojsStore';

  export let media: HTMLMediaElement | null = null;

  let cues: DocumentFragment[] = [];
  let showing: TextTrack | null = null;
  let detach: (() => void) | null = null;

  function render() {
    const active = showing?.activeCues;
    cues = (active ? Array.from(active) : [])
      .map((cue) => cue as VTTCue)
      .filter((cue) => typeof cue.getCueAsHTML === 'function')
      .map((cue) => cue.getCueAsHTML());
  }

  /** Puts one cue's fragment into its (otherwise empty) line element. */
  function cueContent(node: HTMLElement, fragment: DocumentFragment) {
    node.append(fragment);
  }

  function followShowingTrack() {
    const next = media ? (captionTracks(media).find((t) => t.mode === 'showing') ?? null) : null;
    if (next !== showing) {
      showing?.removeEventListener('cuechange', render);
      showing = next;
      showing?.addEventListener('cuechange', render);
    }
    render();
  }

  function attach(element: HTMLMediaElement | null) {
    detach?.();
    detach = null;
    // jsdom's element has no real TextTrackList; a browser's always does.
    const tracks = element?.textTracks;
    if (tracks && typeof tracks.addEventListener === 'function') {
      for (const type of ['change', 'addtrack', 'removetrack']) {
        tracks.addEventListener(type, followShowingTrack);
      }
      detach = () => {
        for (const type of ['change', 'addtrack', 'removetrack']) {
          tracks.removeEventListener(type, followShowingTrack);
        }
        showing?.removeEventListener('cuechange', render);
        showing = null;
      };
    }
    followShowingTrack();
  }

  $: attach(media);

  onDestroy(() => detach?.());
</script>

<div class="caption-overlay" class:has-cues={cues.length > 0}>
  {#each cues as cue (cue)}
    <span class="caption-cue" use:cueContent={cue}></span>
  {/each}
</div>

<style>
  /* The cue box sits over the video, so it is the same dark backdrop in light and dark
     themes; text wraps inside the frame and scales with the player (the container is an
     inline-size container). `unicode-bidi: plaintext` gives every line the direction of its
     own text, so an English cue stays LTR in the Arabic UI and an Arabic cue is RTL in any
     UI (the per-cue equivalent of dir="auto"). */
  .caption-overlay {
    position: absolute;
    inset-inline: 0;
    inset-block-end: var(--space-4);
    display: none;
    flex-direction: column;
    align-items: center;
    padding-inline: 4%;
    font-size: clamp(var(--font-size-md), 2.6cqi, var(--font-size-2xl));
    text-align: center;
    unicode-bidi: plaintext;
    pointer-events: none;
    transition: inset-block-end var(--duration-base) var(--ease-standard);
  }

  .caption-overlay.has-cues {
    display: flex;
  }

  /* Clear the control bar while it is showing. */
  :global(media-container:has(media-controls[data-visible])) .caption-overlay {
    inset-block-end: var(--space-16);
  }

  .caption-cue {
    max-width: 100%;
    padding-inline: var(--space-1);
    background: var(--player-caption-bg);
    color: var(--player-on-media);
    line-height: var(--line-height-snug);
    white-space: pre-line;
    overflow-wrap: anywhere;
    unicode-bidi: plaintext;
    -webkit-box-decoration-break: clone;
    box-decoration-break: clone;
  }

  /* The name is its own bidi island, so the LINE takes the direction of the speech, not of
     the name: an Arabic placeholder label must not turn an English line right-to-left. */
  .caption-overlay :global(.speaker) {
    font-weight: var(--font-weight-semibold);
    color: var(--player-caption-speaker);
    unicode-bidi: plaintext;
  }

  /* The browser's own cue painting is replaced by this overlay. */
  :global(media-container video::cue) {
    visibility: hidden;
  }
</style>
