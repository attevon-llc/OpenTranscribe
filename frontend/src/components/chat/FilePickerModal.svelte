<!--
  FilePickerModal.svelte — choose the transcripts a conversation is grounded in.

  Edits a DRAFT copy of the scope and only commits on Confirm. Changing scope
  mid-conversation changes what every subsequent answer is based on, so an
  accidental click while browsing shouldn't silently rewrite it.

  The footer estimate is fetched from the server as the draft changes, which is
  what stops someone selecting 400 recordings and wondering why answers thin out.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Tabs from '$components/ui/Tabs.svelte';
  import { estimateContext } from '$lib/api/chatApi';
  import { emptyScope, type ChatScope, type ContextEstimate } from '$lib/types/chat';
  import PickerCollectionsTab from './picker/PickerCollectionsTab.svelte';
  import PickerFilesTab from './picker/PickerFilesTab.svelte';
  import PickerSpeakersTab from './picker/PickerSpeakersTab.svelte';
  import PickerTagsTab from './picker/PickerTagsTab.svelte';

  export let isOpen = false;
  export let scope: ChatScope = emptyScope();
  /** The conversation's pinned model; the estimate measures against its window (#1364). */
  export let llmConfigUuid: string | null = null;

  const dispatch = createEventDispatcher<{ confirm: ChatScope; close: void }>();

  let draft: ChatScope = emptyScope();
  let activeTab = 'files';
  let estimate: ContextEstimate | null = null;
  let estimateTimer: ReturnType<typeof setTimeout> | undefined;

  // Re-seed the draft each time the modal opens so a cancelled edit is discarded.
  $: if (isOpen) {
    draft = {
      file_uuids: [...(scope?.file_uuids ?? [])],
      collection_uuids: [...(scope?.collection_uuids ?? [])],
      tag_names: [...(scope?.tag_names ?? [])],
      speakers: [...(scope?.speakers ?? [])],
    };
  }

  $: totalSelected =
    draft.file_uuids.length +
    draft.collection_uuids.length +
    draft.tag_names.length +
    draft.speakers.length;

  $: tabs = [
    { id: 'files', label: $t('chat.picker.tabFiles'), badge: draft.file_uuids.length || null },
    {
      id: 'collections',
      label: $t('chat.picker.tabCollections'),
      badge: draft.collection_uuids.length || null,
    },
    { id: 'tags', label: $t('chat.picker.tabTags'), badge: draft.tag_names.length || null },
    {
      id: 'speakers',
      label: $t('chat.picker.tabSpeakers'),
      badge: draft.speakers.length || null,
    },
  ];

  /** Debounced so dragging through a checkbox list doesn't spam the estimator. */
  function scheduleEstimate(): void {
    clearTimeout(estimateTimer);
    estimateTimer = setTimeout(() => void refreshEstimate(), 350);
  }

  async function refreshEstimate(): Promise<void> {
    try {
      estimate = await estimateContext(draft, llmConfigUuid);
    } catch {
      estimate = null;
    }
  }

  function updateFiles(next: string[]): void {
    draft = { ...draft, file_uuids: next };
    scheduleEstimate();
  }

  function updateCollections(next: string[]): void {
    draft = { ...draft, collection_uuids: next };
    scheduleEstimate();
  }

  function updateTags(next: string[]): void {
    draft = { ...draft, tag_names: next };
    scheduleEstimate();
  }

  function updateSpeakers(next: string[]): void {
    // Speakers narrow within the chosen recordings rather than changing which
    // recordings are searched, so the file-count estimate is unaffected.
    draft = { ...draft, speakers: next };
  }

  function clearAll(): void {
    draft = emptyScope();
    estimate = null;
  }

  function confirm(): void {
    dispatch('confirm', draft);
  }

  function close(): void {
    dispatch('close');
  }
</script>

<BaseModal {isOpen} title={$t('chat.picker.title')} size="lg" onClose={close}>
  <div class="picker">
    <Tabs {tabs} bind:activeId={activeTab} ariaLabel={$t('chat.picker.title')} />

    <div class="tab-panel">
      {#if activeTab === 'files'}
        <PickerFilesTab
          selected={draft.file_uuids}
          on:change={(e) => updateFiles(e.detail)}
        />
      {:else if activeTab === 'collections'}
        <PickerCollectionsTab
          selected={draft.collection_uuids}
          on:change={(e) => updateCollections(e.detail)}
        />
      {:else if activeTab === 'tags'}
        <PickerTagsTab selected={draft.tag_names} on:change={(e) => updateTags(e.detail)} />
      {:else}
        <PickerSpeakersTab
          selected={draft.speakers}
          on:change={(e) => updateSpeakers(e.detail)}
        />
      {/if}
    </div>

    <div class="picker-footer">
      <div class="summary" data-testid="picker-summary">
        {#if totalSelected === 0}
          <span class="all-note">{$t('chat.context.allTranscripts')}</span>
        {:else}
          <span>{$t('chat.picker.selected', { count: totalSelected })}</span>
          {#if estimate}
            <span class="estimate" class:over={estimate.warning_level === 'over'}>
              {#if estimate.pct === null}
                {$t('chat.picker.estimatedFilesUnknown', { files: estimate.file_count })}
              {:else}
                {$t('chat.picker.estimatedFiles', {
                  files: estimate.file_count,
                  pct: Math.round(estimate.pct),
                })}
              {/if}
            </span>
          {/if}
          <button type="button" class="clear-link" on:click={clearAll}>
            {$t('chat.context.clearAll')}
          </button>
        {/if}
      </div>

      <div class="footer-actions">
        <button type="button" class="btn btn-lg btn-secondary" on:click={close}>
          {$t('chat.picker.cancel')}
        </button>
        <button
          type="button"
          class="btn btn-lg btn-primary"
          on:click={confirm}
          data-testid="picker-confirm"
        >
          {$t('chat.picker.confirm')}
        </button>
      </div>
    </div>
  </div>
</BaseModal>

<style>
  .picker {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    min-height: 24rem;
  }

  .tab-panel {
    flex: 1;
    min-height: 0;
  }

  .picker-footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding-top: 0.85rem;
    border-top: 1px solid var(--border-color);
  }

  .summary {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-size: 0.82rem;
    color: var(--text-color);
    flex-wrap: wrap;
  }

  .all-note {
    color: var(--text-secondary);
  }

  .estimate {
    color: var(--text-secondary);
    font-size: 0.78rem;
  }

  .estimate.over {
    color: var(--error-color);
    font-weight: var(--font-weight-semibold);
  }

  .clear-link {
    background: none;
    border: none;
    padding: 0.15rem 0.3rem;
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    font-size: 0.78rem;
    cursor: pointer;
    text-decoration: underline;
  }

  .clear-link:hover {
    color: var(--text-color);
    background-color: var(--button-hover);
  }

  .footer-actions {
    display: flex;
    gap: var(--space-2);
    margin-left: auto;
  }
</style>
