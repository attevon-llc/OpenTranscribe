<script context="module" lang="ts">
  /** Seeks the stub received, in order (FilePlayer.test.ts only). */
  export const seeks: number[] = [];
</script>

<script lang="ts">
  // Test double for VideoPlayer: renders the `#player` element FilePlayer waits for and
  // records seeks. Props mirror the ones FilePlayer passes.
  export let videoUrl = '';

  export function seekToTime(time: number) {
    seeks.push(time);
  }
</script>

{#if videoUrl}
  <div id="player"></div>
{/if}
