<!--
  AudioTracksNotice.svelte — the file page's audio-track panel (#1263).

  For a multi-track file it lists the tracks and marks the one that was transcribed. When the
  detected language disagrees with that track's language tag it adds a calm, non-blocking
  notice offering to re-process with another track. A single-track file renders nothing.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { locale, t } from '$stores/locale';
  import {
    describeTrack,
    languageName,
    selectableTracks,
    type AudioStream,
  } from '$lib/fileDetail/audioTracks';

  export let streams: AudioStream[] | null | undefined = null;
  /** The track chosen for transcription; null = the first. */
  export let chosen: number | null | undefined = null;
  /** The language the speech was detected as (a code such as `en`). */
  export let detectedLanguage: string | null | undefined = null;
  /** The server's verdict that the detected language and the track's tag disagree. */
  export let mismatch = false;
  /** The transcribed track's language tag, for the notice. */
  export let trackLanguage: string | null | undefined = null;
  /** Re-processing is unavailable (e.g. the file is held or the viewer cannot edit). */
  export let canReprocess = true;

  const dispatch = createEventDispatcher<{ chooseTrack: void }>();

  $: tracks = selectableTracks(streams);
  $: active = chosen ?? 0;
  $: taggedName = languageName(trackLanguage, $locale) ?? '';
  $: detectedName = languageName(detectedLanguage, $locale) ?? '';
</script>

{#if tracks.length || mismatch}
  <section class="audio-tracks" data-testid="audio-tracks">
    {#if tracks.length}
      <h4 class="tracks-heading">{$t('audioTracks.title')}</h4>
      <ul class="tracks-list" aria-label={$t('audioTracks.listLabel')}>
        {#each tracks as track (track.index)}
          <li class="track" class:active={track.index === active}>
            <span class="track-number">{$t('audioTracks.trackNumber', { number: track.index + 1 })}</span>
            <span class="track-desc">{describeTrack(track, $t, $locale)}</span>
            {#if track.index === active}
              <span class="track-badge">{$t('audioTracks.transcribed')}</span>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    {#if mismatch}
      <div class="mismatch" role="note" data-testid="audio-language-mismatch">
        <p class="mismatch-title">{$t('audioTracks.mismatchTitle')}</p>
        <p class="mismatch-body">
          {$t('audioTracks.mismatchBody', { tagged: taggedName, detected: detectedName })}
        </p>
        {#if tracks.length && canReprocess}
          <button type="button" class="mismatch-action" on:click={() => dispatch('chooseTrack')}>
            {$t('audioTracks.chooseAnother')}
          </button>
        {/if}
      </div>
    {/if}
  </section>
{/if}

<style>
  .audio-tracks {
    margin-bottom: var(--space-5);
    padding: var(--space-3) var(--space-4);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
  }

  .tracks-heading {
    margin: 0 0 var(--space-2);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
  }

  .tracks-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .track {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .track.active {
    color: var(--text-primary);
  }

  .track-number {
    font-weight: var(--font-weight-semibold);
  }

  .track-badge {
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-pill);
    font-size: var(--font-size-xs);
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
    color: var(--text-primary);
  }

  .mismatch {
    margin-top: var(--space-3);
    padding: var(--space-2-5) var(--space-3);
    border-radius: var(--radius-md);
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
  }

  .mismatch-title {
    margin: 0 0 var(--space-0-5);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
  }

  .mismatch-body {
    margin: 0;
    font-size: var(--font-size-sm);
    line-height: 1.45;
    color: var(--text-secondary);
  }

  .mismatch-action {
    margin-top: var(--space-2);
    padding: var(--space-1) var(--space-3);
    font-size: var(--font-size-sm);
    cursor: pointer;
    background: transparent;
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
  }

  .mismatch-action:hover {
    background: var(--surface-hover);
  }
</style>
