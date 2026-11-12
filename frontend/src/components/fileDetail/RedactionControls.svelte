<script lang="ts">
  /**
   * Content-redaction controls rendered BELOW the transcript so the video and
   * transcript columns stay top-aligned.
   *
   * Renders nothing when the viewer can neither toggle nor rescan.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import FrozenNote from '$components/legal-hold/FrozenNote.svelte';
  import { fileFrozen } from '$lib/legalHold/fileHoldContext';

  export let showRedactionToggle = false;
  export let canViewOriginal = false;
  // Backend-decided (`redaction_enabled`): with redaction off, a scan changes nothing the
  // viewer can see, so neither the "not redacted" status nor the run button is offered.
  export let redactionEnabled = false;
  // `done` with no spans means the scan ran and found nothing for the enabled categories.
  export let redactionStatus = '';
  export let showOriginal = false;
  export let redactionToggleBusy = false;

  const dispatch = createEventDispatcher();
  // An explicit rescan of a held file is refused (legal-hold plan §3.4); viewing either version
  // still works.
  const frozen = fileFrozen();
</script>

{#if showRedactionToggle}
  <div class="redaction-footer">
    <span class="redaction-status">
      {showOriginal
        ? $t('settings.contentRedaction.showingOriginal')
        : $t('settings.contentRedaction.showingRedacted')}
    </span>
    <div class="redaction-bar-actions">
      {#if canViewOriginal && redactionEnabled}
        <button
          type="button"
          class="redaction-link-btn"
          on:click={() => dispatch('rescan')}
          disabled={$frozen}
          aria-describedby={$frozen ? 'redaction-frozen-note' : undefined}
          title={$frozen ? $t('holds.frozenControl') : $t('settings.contentRedaction.rescanTooltip')}
        >
          {$t('settings.contentRedaction.rescan')}
        </button>
      {/if}
      <button
        type="button"
        class="redaction-link-btn"
        on:click={() => dispatch('toggleOriginal')}
        disabled={redactionToggleBusy}
        title={showOriginal
          ? $t('settings.contentRedaction.showRedactedTooltip')
          : $t('settings.contentRedaction.showOriginalTooltip')}
      >
        {#if redactionToggleBusy}
          <Spinner size="small" />
        {/if}
        {showOriginal
          ? $t('settings.contentRedaction.showRedacted')
          : $t('settings.contentRedaction.showOriginal')}
      </button>
    </div>
  </div>
{:else if canViewOriginal && redactionEnabled}
  <div class="redaction-footer">
    <span class="redaction-status">
      {redactionStatus === 'done'
        ? $t('settings.contentRedaction.scanNothingFound')
        : $t('settings.contentRedaction.notRedacted')}
    </span>
    <button
      type="button"
      class="redaction-link-btn"
      on:click={() => dispatch('rescan')}
      disabled={$frozen}
      aria-describedby={$frozen ? 'redaction-frozen-note' : undefined}
      title={$frozen ? $t('holds.frozenControl') : $t('settings.contentRedaction.rescanTooltip')}
    >
      {$t('settings.contentRedaction.runRedaction')}
    </button>
  </div>
{/if}
{#if $frozen && canViewOriginal && redactionEnabled}
  <FrozenNote id="redaction-frozen-note" />
{/if}

<style>
  /* Compact redaction controls placed under the transcript (keeps columns top-aligned). */
  .redaction-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-1) 0;
    margin-top: var(--space-2);
    border-top: 1px solid var(--border-color);
    flex-wrap: wrap;
  }
  .redaction-status {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }
  .redaction-bar-actions {
    display: flex;
    gap: var(--space-3-5);
    align-items: center;
  }
  /* Small, unobtrusive link-style button. Hover shifts to the primary-hover color with
     a subtle tinted background (consistent with the app) — no underline. */
  .redaction-link-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    background: none;
    border: none;
    padding: var(--space-0-5) var(--space-1-5);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--primary-on-surface);
    cursor: pointer;
    white-space: nowrap;
    transition:
      color var(--duration-fast) ease,
      background-color var(--duration-fast) ease;
  }
  .redaction-link-btn:hover {
    color: var(--link-hover);
    background-color: rgba(var(--primary-color-rgb), 0.1);
  }
  .redaction-link-btn:disabled {
    opacity: 0.6;
    cursor: wait;
  }
</style>
