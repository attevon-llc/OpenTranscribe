<script lang="ts">
  import type { Segment } from '$lib/types/speaker';
  import { createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { mergeSpeakers } from '$lib/api/speakerClusters';
  import { toastStore } from '$stores/toast';
  import { getSpeakerColor } from '$lib/utils/speakerColors';
  import type { Speaker } from '$lib/types/speaker';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { translateSpeakerLabel } from '$lib/i18n';
  import BaseModal from './ui/BaseModal.svelte';
  import Spinner from './ui/Spinner.svelte';

  export let speakers: Speaker[] = [];
  export let transcriptSegments: Segment[] = []; // Transcript segments for live segment counting

  const dispatch = createEventDispatcher();

  // Collapsible state - default to collapsed since merge is used less frequently
  let isCollapsed = true;

  // Track selected speakers
  let selectedSpeakers = new Set<string>();
  let showTargetDialog = false;
  let targetSpeaker: Speaker | null = null;
  let merging = false;

  // Reactive: Get list of selected speaker objects
  $: selectedSpeakerList = speakers.filter(s => selectedSpeakers.has(s.uuid));

  // Reactive: Enable merge button only when 2+ speakers selected
  $: canMerge = selectedSpeakers.size >= 2;

  // Reactive: Calculate segment counts from transcript data for live updates
  $: segmentCountMap = (() => {
    const counts = new Map<string, number>();
    if (transcriptSegments && transcriptSegments.length > 0) {
      for (const segment of transcriptSegments) {
        const speakerId = segment.speaker?.uuid || segment.speaker_id;
        if (speakerId) {
          counts.set(speakerId, (counts.get(speakerId) || 0) + 1);
        }
      }
    }
    return counts;
  })();

  // Get segment count for a speaker (live from transcript or fallback to stored value)
  function getSegmentCount(speaker: Speaker): number {
    // Prefer live count from transcript segments
    const liveCount = segmentCountMap.get(speaker.uuid);
    if (liveCount !== undefined) {
      return liveCount;
    }
    // Fallback to stored value
    return speaker.segment_count || 0;
  }

  // Toggle speaker selection
  function toggleSpeaker(speakerUuid: string) {
    if (selectedSpeakers.has(speakerUuid)) {
      selectedSpeakers.delete(speakerUuid);
    } else {
      selectedSpeakers.add(speakerUuid);
    }
    selectedSpeakers = new Set(selectedSpeakers); // Trigger reactivity
  }

  // Open target selection dialog
  function openTargetDialog() {
    if (!canMerge) return;
    showTargetDialog = true;
    targetSpeaker = null;
  }

  // Close target selection dialog
  function closeTargetDialog() {
    showTargetDialog = false;
    targetSpeaker = null;
  }

  // Perform the merge operation
  async function performMerge() {
    if (!targetSpeaker || selectedSpeakers.size < 2) {
      toastStore.error($t('speaker.pleaseSelectTarget'));
      return;
    }
    const target = targetSpeaker;

    merging = true;

    // Get source speakers (all selected except target)
    const sourceSpeakers = selectedSpeakerList.filter(s => s.uuid !== target.uuid);

    // Track successful and failed merges
    const successfulMerges: Speaker[] = [];
    const failedMerges: { speaker: Speaker; error: string }[] = [];

    // Merge each source speaker into target. Concurrent, not serial: merging k speakers
    // took k round trips end to end. `allSettled` keeps the same per-source success/failure
    // accounting the messages below depend on.
    const outcomes = await Promise.allSettled(
      sourceSpeakers.map(sourceSpeaker => mergeSpeakers(sourceSpeaker.uuid, target.uuid))
    );

    outcomes.forEach((outcome, i) => {
      const sourceSpeaker = sourceSpeakers[i];
      if (outcome.status === 'fulfilled') {
        successfulMerges.push(sourceSpeaker);
      } else {
        const errorMessage = getErrorMessage(outcome.reason, $t('common.unknownError'));
        failedMerges.push({ speaker: sourceSpeaker, error: errorMessage });
        console.error(`Error merging speaker ${sourceSpeaker.display_name || sourceSpeaker.name}:`, outcome.reason);
      }
    });

    // Show appropriate message based on results
    if (failedMerges.length === 0) {
      // All merges succeeded
      const speakerWord = successfulMerges.length > 1 ? $t('speaker.speakers') : $t('speaker.speaker');
      const targetName = target.display_name || target.name;
      toastStore.success(
        $t('speaker.mergeSuccessAll', { count: successfulMerges.length, speakerWord, target: targetName })
      );
    } else if (successfulMerges.length === 0) {
      // All merges failed
      const failedNames = failedMerges.map(f => f.speaker.display_name || f.speaker.name).join(', ');
      toastStore.error($t('speaker.mergeFailedAll', { names: failedNames, error: failedMerges[0].error }));
    } else {
      // Partial success - some merged, some failed
      const successNames = successfulMerges.map(s => s.display_name || s.name).join(', ');
      const failedNames = failedMerges.map(f => f.speaker.display_name || f.speaker.name).join(', ');
      toastStore.warning(
        $t('speaker.mergePartial', { successNames, failedNames })
      );
    }

    // Reset selection
    selectedSpeakers.clear();
    selectedSpeakers = new Set(selectedSpeakers);
    closeTargetDialog();

    // Dispatch merged event to parent if any merges succeeded
    if (successfulMerges.length > 0) {
      dispatch('merged');
    }

    merging = false;
  }

  // Clear all selections
  function clearSelection() {
    selectedSpeakers.clear();
    selectedSpeakers = new Set(selectedSpeakers);
  }
</script>

<div class="speaker-merge">
  <button
    class="merge-header-toggle"
    on:click={() => isCollapsed = !isCollapsed}
    aria-expanded={!isCollapsed}
    aria-controls="merge-content"
  >
    <div class="merge-header-left">
      <h5>{$t('speaker.mergeSpeakers')}</h5>
      <span class="merge-header-hint">{$t('speaker.mergeSpeakersHint')}</span>
    </div>
    <svg
      class="chevron-icon"
      class:rotated={!isCollapsed}
      xmlns="http://www.w3.org/2000/svg"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>
  </button>

  {#if !isCollapsed}
    <div id="merge-content" class="merge-content" transition:slide={{ duration: 200 }}>
      <p class="help-text">{$t('speaker.helpText')}</p>

      <div class="speaker-grid">
        {#each speakers as speaker}
          <label class="speaker-card" class:selected={selectedSpeakers.has(speaker.uuid)}>
            <input
              type="checkbox"
              checked={selectedSpeakers.has(speaker.uuid)}
              on:change={() => toggleSpeaker(speaker.uuid)}
            />
            <div class="speaker-info">
              <span
                class="speaker-badge"
                style="background-color: {getSpeakerColor(speaker.name).bg}; border-color: {getSpeakerColor(speaker.name).border}; --speaker-light: {getSpeakerColor(speaker.name).textLight}; --speaker-dark: {getSpeakerColor(speaker.name).textDark};"
              >
                {translateSpeakerLabel(speaker.display_name || speaker.name)}
              </span>
              <span class="segment-count">
                {getSegmentCount(speaker)} {getSegmentCount(speaker) !== 1 ? $t('speaker.segments') : $t('speaker.segment')}
              </span>
            </div>
          </label>
        {/each}
      </div>

      <div class="merge-actions">
        <button
          class="btn-secondary"
          on:click={clearSelection}
          disabled={selectedSpeakers.size === 0}
        >
          {$t('speaker.clearSelection')}
        </button>
        <button
          class="btn-primary"
          on:click={openTargetDialog}
          disabled={!canMerge}
          title={canMerge ? $t('speaker.selectTargetAndMerge') : $t('speaker.selectAtLeastTwo')}
        >
          {#if selectedSpeakers.size === 0}
            {$t('speaker.mergeSelected', { count: 0 })}
          {:else if selectedSpeakers.size === 1}
            {$t('speaker.mergeSelectedNeedMore', { count: 1 })}
          {:else}
            {$t('speaker.mergeSelected', { count: selectedSpeakers.size })}
          {/if}
        </button>
      </div>
    </div>
  {/if}

  <BaseModal isOpen={showTargetDialog} title={$t('speaker.selectTargetSpeaker')} onClose={closeTargetDialog} maxWidth="500px">
    <p class="modal-description">
      {$t('speaker.modalDescription', {
        count: selectedSpeakers.size - 1,
        speakerWord: selectedSpeakers.size - 1 !== 1 ? $t('speaker.speakers') : $t('speaker.speaker')
      })}
    </p>

    <div class="target-list">
      {#each selectedSpeakerList as speaker}
        <label class="target-option" class:selected={targetSpeaker?.uuid === speaker.uuid}>
          <input
            type="radio"
            name="target-speaker"
            value={speaker.uuid}
            on:change={() => targetSpeaker = speaker}
          />
          <div class="target-info">
            <span
              class="speaker-badge"
              style="background-color: {getSpeakerColor(speaker.name).bg}; border-color: {getSpeakerColor(speaker.name).border}; --speaker-light: {getSpeakerColor(speaker.name).textLight}; --speaker-dark: {getSpeakerColor(speaker.name).textDark};"
            >
              {translateSpeakerLabel(speaker.display_name || speaker.name)}
            </span>
            <span class="segment-count">{getSegmentCount(speaker)} {$t('speaker.segments')}</span>
          </div>
        </label>
      {/each}
    </div>

    <svelte:fragment slot="footer">
      <button class="btn-secondary" on:click={closeTargetDialog} disabled={merging}>
        {$t('common.cancel')}
      </button>
      <button
        class="btn-primary"
        on:click={performMerge}
        disabled={!targetSpeaker || merging}
      >
        {#if merging}
          <Spinner size="small" color="currentColor" />
          {$t('speaker.merging')}
        {:else if targetSpeaker}
          {$t('speaker.confirmMerge', { name: targetSpeaker.display_name || targetSpeaker.name })}
        {:else}
          {$t('speaker.selectTargetSpeaker')}
        {/if}
      </button>
    </svelte:fragment>
  </BaseModal>
</div>

<style>
  .speaker-merge {
    margin-bottom: 1.5rem;
    background: var(--background-secondary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
  }

  .merge-header-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: 0.875rem 1rem;
    background: transparent;
    border: none;
    cursor: pointer;
    transition: background-color 0.2s ease;
  }

  .merge-header-toggle:hover {
    background: var(--surface-hover);
  }

  .merge-header-left {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.125rem;
  }

  .merge-header-left h5 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .merge-header-hint {
    font-size: 11px;
    color: var(--text-secondary);
  }

  .chevron-icon {
    color: var(--text-secondary);
    transition: transform 0.2s ease;
    flex-shrink: 0;
  }

  .chevron-icon.rotated {
    transform: rotate(180deg);
  }

  .merge-content {
    padding: 0 1rem 1rem;
  }

  .help-text {
    margin: 0 0 1rem 0;
    font-size: 12px;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .speaker-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 0.75rem;
    margin-bottom: 1rem;
  }

  .speaker-card {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem;
    background: var(--surface-color);
    border: 2px solid var(--border-color);
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .speaker-card:hover {
    border-color: var(--primary-color);
    background: var(--surface-hover);
  }

  .speaker-card.selected {
    border-color: var(--primary-color);
    background: rgba(59, 130, 246, 0.1);
  }

  .speaker-card input[type="checkbox"] {
    cursor: pointer;
    width: 18px;
    height: 18px;
    accent-color: var(--primary-color);
  }

  .speaker-info {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
  }

  .speaker-badge {
    font-size: 11px;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 12px;
    border: 1px solid;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--speaker-light);
    display: inline-block;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global([data-theme='dark']) .speaker-badge {
    color: var(--speaker-dark);
  }

  .segment-count {
    font-size: 11px;
    color: var(--text-secondary);
  }

  .merge-actions {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
  }

  .btn-primary,
  .btn-secondary {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    border: none;
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
  }

  .btn-primary {
    background-color: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover:not(:disabled) {
    background-color: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-primary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
  }

  .btn-secondary {
    background-color: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    box-shadow: var(--card-shadow);
  }

  .btn-secondary:hover:not(:disabled) {
    background-color: var(--button-hover);
    border-color: var(--border-color);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
  }

  .btn-secondary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .modal-description {
    margin: 0 0 1rem 0;
    font-size: 14px;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .target-list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .target-option {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem;
    background: var(--background-secondary);
    border: 2px solid var(--border-color);
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .target-option:hover {
    border-color: var(--primary-color);
    background: var(--surface-hover);
  }

  .target-option.selected {
    border-color: var(--primary-color);
    background: rgba(59, 130, 246, 0.1);
  }

  .target-option input[type="radio"] {
    cursor: pointer;
    width: 18px;
    height: 18px;
    accent-color: var(--primary-color);
  }

  .target-info {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  @media (max-width: 768px) {
    .speaker-grid {
      grid-template-columns: 1fr;
    }

    .merge-actions {
      flex-direction: column;
    }
  }
</style>
