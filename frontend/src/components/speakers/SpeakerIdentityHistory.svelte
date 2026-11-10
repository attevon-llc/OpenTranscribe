<!--
  SpeakerIdentityHistory.svelte — a speaker's recorded name, link and suggestion changes
  (issue #1336), behind a small toggle. Loads only when opened, and again on each open, so a
  change made meanwhile shows up. Read-only: the rows are written by the backend with each
  change. Rules (which value to show, how to name who acted) are `$lib/speakers/identityHistory`.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import { getSpeakerIdentityHistory } from '$lib/api/speakerHistory';
  import { formatDateTime } from '$lib/utils/formatting';
  import {
    actorOf,
    changeOf,
    confidencePercent,
    kindKey,
    type HistoryValue,
  } from '$lib/speakers/identityHistory';
  import type { SpeakerIdentityEvent } from '$lib/types/speaker';

  export let speakerUuid: string;

  let open = false;
  let loading = false;
  let failed = false;
  let events: SpeakerIdentityEvent[] = [];
  let requested = 0;

  async function load(): Promise<void> {
    const ticket = ++requested;
    loading = true;
    failed = false;
    try {
      const rows = await getSpeakerIdentityHistory(speakerUuid);
      if (ticket === requested) events = rows;
    } catch {
      if (ticket === requested) failed = true;
    } finally {
      if (ticket === requested) loading = false;
    }
  }

  function toggle(): void {
    open = !open;
    if (open) void load();
  }

  function show(value: HistoryValue): string {
    if (!value) return '';
    return 'text' in value ? value.text : $t(value.key);
  }

  // A name keeps its own direction inside the sentence (U+2068 FSI ... U+2069 PDI), so the
  // sentence reads in the locale's direction, not the names' (ar is RTL).
  const FIRST_STRONG_ISOLATE = String.fromCodePoint(0x2068);
  const POP_DIRECTIONAL_ISOLATE = String.fromCodePoint(0x2069);

  function isolated(value: HistoryValue): string {
    return FIRST_STRONG_ISOLATE + show(value) + POP_DIRECTIONAL_ISOLATE;
  }
</script>

<div class="identity-history">
  <button
    type="button"
    class="history-toggle"
    aria-expanded={open}
    on:click={toggle}
    title={$t('speakerHistory.toggleTitle')}
  >
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" class:rotated={open}>
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>
    {$t('speakerHistory.toggle')}
  </button>

  {#if open}
    <div class="history-body" role="region" aria-label={$t('speakerHistory.title')}>
      {#if loading && events.length === 0}
        <p class="history-note">{$t('speakerHistory.loading')}</p>
      {:else if failed}
        <p class="history-note history-error">{$t('speakerHistory.loadFailed')}</p>
      {:else if events.length === 0}
        <p class="history-note">{$t('speakerHistory.empty')}</p>
      {:else}
        <ol class="history-list">
          {#each events as event (event.uuid)}
            {@const change = changeOf(event)}
            {@const percent = confidencePercent(event)}
            <li class="history-entry">
              <div class="history-what">
                <span class="history-kind">{$t(kindKey(event))}</span>
                {#if change}
                  <span class="history-change">
                    {#if change.to}
                      {$t('speakerHistory.change', {
                        from: isolated(change.from),
                        to: isolated(change.to),
                      })}
                    {:else}
                      {show(change.from)}
                    {/if}
                  </span>
                {/if}
              </div>
              <div class="history-meta">
                <span>{show(actorOf(event))}</span>
                <span aria-hidden="true">·</span>
                <time datetime={event.at}>{formatDateTime(event.at)}</time>
                {#if event.model}
                  <span aria-hidden="true">·</span>
                  <span>{$t('speakerHistory.model', { model: event.model })}</span>
                {/if}
                {#if percent !== null}
                  <span aria-hidden="true">·</span>
                  <span>{$t('speakerHistory.confidence', { percent })}</span>
                {/if}
              </div>
            </li>
          {/each}
        </ol>
      {/if}
    </div>
  {/if}
</div>

<style>
  .identity-history {
    margin-top: var(--space-1-5);
  }

  .history-toggle {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-0-5) 0;
    background: none;
    border: none;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    cursor: pointer;
  }

  .history-toggle:hover,
  .history-toggle:focus-visible {
    color: var(--primary-on-surface);
  }

  .history-toggle svg {
    transition: transform var(--duration-fast) var(--ease-standard);
  }

  .history-toggle svg.rotated {
    transform: rotate(180deg);
  }

  .history-body {
    margin-top: var(--space-1);
    padding-inline-start: var(--space-3);
    border-inline-start: 2px solid var(--border-color);
  }

  .history-note {
    margin: var(--space-1) 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .history-error {
    color: var(--error-text);
  }

  .history-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
    max-height: calc(var(--space-16) * 3.5);
    overflow-y: auto;
  }

  .history-what {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .history-kind {
    font-weight: var(--font-weight-semibold);
  }

  .history-meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
  }
</style>
