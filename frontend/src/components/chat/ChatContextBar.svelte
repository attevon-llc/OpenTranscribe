<!--
  ChatContextBar.svelte — what this conversation is grounded in.

  Always visible above the composer, because "which transcripts am I asking
  about?" is the question users get wrong most often. An empty scope is shown
  explicitly as "All transcripts" rather than as absence, and context-off mode
  gets its own unmistakable chip — an answer with no transcripts behind it must
  never look like one that has them.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { isScopeEmpty, type ChatScope, type ContextEstimate } from '$lib/types/chat';

  export let scope: ChatScope;
  export let useContext = true;
  export let estimate: ContextEstimate | null = null;
  export let disabled = false;

  const dispatch = createEventDispatcher<{
    openPicker: void;
    clear: void;
    toggleContext: boolean;
    removeSpeaker: string;
  }>();

  $: allTranscripts = isScopeEmpty(scope);
  $: fileCount = scope?.file_uuids?.length ?? 0;
  $: collectionCount = scope?.collection_uuids?.length ?? 0;
  $: tagCount = scope?.tag_names?.length ?? 0;
  $: speakerNames = scope?.speakers ?? [];
  // Recordings AND speakers are both real scope, even though `isScopeEmpty`
  // only looks at the recording axis (files/collections/tags) — a
  // speakers-only scope ("everything Dana said, anywhere") must still offer
  // a way to clear it, or it is a filter with no way back to "all transcripts".
  $: hasAnyScope = !allTranscripts || speakerNames.length > 0;
</script>

<div class="context-bar" data-testid="chat-context-bar">
  {#if !useContext}
    <span class="chip chip-off" data-testid="chat-context-off">
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        aria-hidden="true"
      >
        <line x1="1" y1="1" x2="23" y2="23" />
        <path d="M10.58 10.58a2 2 0 0 0 2.83 2.83" />
        <path d="M9.36 5.05A9.46 9.46 0 0 1 12 4.5c7 0 10 7.5 10 7.5a13.16 13.16 0 0 1-1.67 2.68" />
        <path d="M6.06 6.06A13.16 13.16 0 0 0 2 12s3 7.5 10 7.5a9.46 9.46 0 0 0 2.64-.55" />
      </svg>
      {$t('chat.controls.contextOff')}
    </span>
  {:else if allTranscripts}
    <span class="chip" data-testid="chat-scope-all">
      {$t('chat.context.allTranscripts')}
    </span>
  {:else}
    {#if fileCount > 0}
      <span class="chip chip-scoped" data-testid="chat-scope-files">
        {$t('chat.context.filesCount', { count: fileCount })}
      </span>
    {/if}
    {#if collectionCount > 0}
      <span class="chip chip-scoped" data-testid="chat-scope-collections">
        {$t('chat.context.collectionsCount', { count: collectionCount })}
      </span>
    {/if}
    {#if tagCount > 0}
      <span class="chip chip-scoped" data-testid="chat-scope-tags">
        {$t('chat.context.tagsCount', { count: tagCount })}
      </span>
    {/if}
  {/if}

  {#if useContext}
    {#each speakerNames as name (name)}
      <span class="chip chip-speaker" data-testid="chat-scope-speaker">
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          aria-hidden="true"
        >
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
        <span class="speaker-name">{name}</span>
        <button
          type="button"
          class="chip-remove"
          on:click={() => dispatch('removeSpeaker', name)}
          {disabled}
          aria-label={$t('chat.context.removeSpeaker', { name })}
          data-testid="chat-scope-speaker-remove"
        >
          &times;
        </button>
      </span>
    {/each}
  {/if}

  {#if useContext && hasAnyScope}
    <button
      type="button"
      class="text-action"
      on:click={() => dispatch('clear')}
      {disabled}
      data-testid="chat-scope-clear"
    >
      {$t('chat.context.clearAll')}
    </button>
  {/if}

  {#if useContext && estimate && estimate.warning_level !== 'ok'}
    <span
      class="chip chip-estimate"
      class:over={estimate.warning_level === 'over'}
      data-testid="chat-context-estimate"
    >
      {$t('chat.context.estimate', { pct: Math.round(estimate.pct) })}
    </span>
  {/if}

  <button
    type="button"
    class="add-context"
    on:click={() => dispatch('openPicker')}
    disabled={disabled || !useContext}
    data-testid="chat-add-context"
  >
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      aria-hidden="true"
    >
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
    {$t('chat.context.addContext')}
  </button>
</div>

<style>
  .context-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem;
    padding: 0.5rem 0;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.22rem 0.6rem;
    border-radius: 999px;
    border: 1px solid var(--border-color);
    background-color: var(--surface-color);
    color: var(--text-secondary);
    font-size: 0.76rem;
    white-space: nowrap;
  }

  .chip-scoped {
    border-color: rgba(var(--primary-color-rgb), 0.35);
    background-color: rgba(var(--primary-color-rgb), 0.08);
    color: var(--primary-color);
    font-weight: 500;
  }

  .chip-off {
    border-color: rgba(var(--warning-color-rgb, 255, 193, 7), 0.5);
    background-color: rgba(var(--warning-color-rgb, 255, 193, 7), 0.12);
    color: var(--text-color);
    font-weight: 500;
  }

  .chip-speaker {
    border-color: rgba(var(--primary-color-rgb), 0.35);
    background-color: rgba(var(--primary-color-rgb), 0.08);
    color: var(--primary-color);
    font-weight: 500;
    max-width: 16rem;
  }

  .speaker-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chip-remove {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: none;
    width: 1rem;
    height: 1rem;
    padding: 0;
    margin: 0 -0.15rem 0 0.05rem;
    border: none;
    border-radius: 50%;
    background: none;
    color: inherit;
    font-size: 0.9rem;
    line-height: 1;
    cursor: pointer;
  }

  .chip-remove:hover:not(:disabled) {
    background-color: rgba(var(--primary-color-rgb), 0.2);
  }

  .chip-remove:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .chip-estimate {
    border-color: rgba(var(--warning-color-rgb, 255, 193, 7), 0.5);
    color: var(--text-color);
  }

  .chip-estimate.over {
    border-color: rgba(var(--error-color-rgb, 220, 53, 69), 0.5);
    color: var(--error-color, #dc3545);
  }

  .add-context,
  .text-action {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.22rem 0.55rem;
    border: none;
    border-radius: 6px;
    background: none;
    color: var(--text-secondary);
    font-size: 0.76rem;
    cursor: pointer;
  }

  .add-context:hover:not(:disabled),
  .text-action:hover:not(:disabled) {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .add-context:disabled,
  .text-action:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
