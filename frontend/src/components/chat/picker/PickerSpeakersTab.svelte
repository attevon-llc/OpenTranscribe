<!--
  PickerSpeakersTab.svelte — narrow the conversation to specific speakers.

  The transcript-native filter, and the one that has no equivalent in a generic
  document chat. Because chunks are speaker turns, selecting a speaker makes
  retrieval return *only that person's words* — so "what did Dana commit to?"
  cannot be answered with someone else's sentence about Dana.

  This is a DIFFERENT axis from the other tabs: recordings/collections/tags pick
  which files to search, speakers pick who to listen to within them. Both can be
  combined, and speakers alone is valid ("everything Dana said, anywhere").
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';

  export let selected: string[] = [];

  const dispatch = createEventDispatcher<{ change: string[] }>();

  interface PickerSpeaker {
    uuid: string;
    /** Display name — this is what the search index stores on each chunk. */
    display_name: string;
    name?: string;
    media_count?: number;
  }

  let speakers: PickerSpeaker[] = [];
  let loading = true;
  let filter = '';

  $: selectedSet = new Set(selected);
  $: visible = filter.trim()
    ? speakers.filter((s) =>
        (s.display_name || '').toLowerCase().includes(filter.trim().toLowerCase())
      )
    : speakers;

  onMount(async () => {
    try {
      // for_filter returns speakers deduplicated by display name with counts,
      // already tenant-gated and share-aware — and those display names are
      // exactly the values indexed on each chunk, so the filter matches.
      const { data } = await axiosInstance.get('/speakers', { params: { for_filter: true } });
      const rows: PickerSpeaker[] = Array.isArray(data) ? data : (data?.items ?? []);
      speakers = rows.filter((s) => s.display_name);
    } catch {
      speakers = [];
    } finally {
      loading = false;
    }
  });

  function toggle(name: string): void {
    dispatch(
      'change',
      selectedSet.has(name) ? selected.filter((n) => n !== name) : [...selected, name]
    );
  }
</script>

<div class="picker-tab">
  {#if loading}
    <div class="loading"><Spinner size="small" /></div>
  {:else}
    <p class="tab-hint">{$t('chat.picker.speakersHint')}</p>

    <input
      class="speaker-filter"
      type="search"
      bind:value={filter}
      placeholder={$t('chat.picker.searchSpeakers')}
      aria-label={$t('chat.picker.searchSpeakers')}
    />

    <ul class="picker-list" data-testid="picker-speakers-list">
      {#each visible as speaker (speaker.uuid)}
        <li>
          <label class="picker-row">
            <input
              type="checkbox"
              checked={selectedSet.has(speaker.display_name)}
              on:change={() => toggle(speaker.display_name)}
              data-testid="picker-speaker-checkbox"
            />
            <span class="row-label">{speaker.display_name}</span>
            {#if speaker.media_count !== undefined}
              <span class="row-count">
                {$t('chat.picker.speakerFileCount', { count: speaker.media_count })}
              </span>
            {/if}
          </label>
        </li>
      {/each}

      {#if visible.length === 0}
        <li class="empty">{$t('chat.picker.emptySpeakers')}</li>
      {/if}
    </ul>
  {/if}
</div>

<style>
  .picker-tab {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    min-height: 0;
  }

  .loading {
    display: flex;
    justify-content: center;
    padding: 2rem 0;
  }

  .tab-hint {
    margin: 0;
    font-size: 0.78rem;
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .speaker-filter {
    width: 100%;
    padding: 0.45rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.87rem;
  }

  .speaker-filter:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .picker-list {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    max-height: 38vh;
  }

  .picker-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.4rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.87rem;
    color: var(--text-color);
  }

  .picker-row:hover {
    background-color: var(--button-hover);
  }

  .row-label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-count {
    flex-shrink: 0;
    font-size: 0.74rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .empty {
    padding: 1.5rem 0.5rem;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.85rem;
  }
</style>
