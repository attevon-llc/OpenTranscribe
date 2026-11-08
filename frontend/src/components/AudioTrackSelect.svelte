<!--
  AudioTrackSelect.svelte — which audio track of a multi-track file to transcribe (#1263).

  Each option is labelled from what ffprobe found in the container (language tag, channels,
  codec, title). The chosen track drives the transcript, the speakers and the playback audio.
  Renders nothing for a file with fewer than two tracks, so a single-track file looks exactly
  as it did before.
-->
<script lang="ts">
  import { locale, t } from '$stores/locale';
  import {
    selectableTracks,
    trackOptionLabel,
    type AudioStream,
  } from '$lib/fileDetail/audioTracks';

  /** The file's tracks, as the server stored them. */
  export let streams: AudioStream[] | null | undefined = null;
  /** The chosen track index. Bindable; null until the user (or the file's current choice) sets it. */
  export let value: number | null = null;
  export let id = 'audio-track-select';

  $: tracks = selectableTracks(streams);
</script>

{#if tracks.length}
  <div class="setting-field audio-track" data-testid="audio-track-select">
    <label for={id}>{$t('audioTracks.selectLabel')}</label>
    <select {id} bind:value class="track-select">
      {#each tracks as track (track.index)}
        <option value={track.index}>{trackOptionLabel(track, $t, $locale)}</option>
      {/each}
    </select>
    <p class="track-hint">{$t('audioTracks.selectHint')}</p>
  </div>
{/if}

<style>
  .audio-track {
    margin-bottom: var(--space-4);
  }

  .track-select {
    width: 100%;
  }

  .track-hint {
    margin: var(--space-1-5) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.4;
  }
</style>
