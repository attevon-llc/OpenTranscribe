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

  export let showRedactionToggle = false;
  export let canViewOriginal = false;
  export let showOriginal = false;
  export let redactionToggleBusy = false;

  const dispatch = createEventDispatcher();
</script>

{#if showRedactionToggle}
  <div class="redaction-footer">
    <span class="redaction-status">
      {showOriginal
        ? $t('settings.contentRedaction.showingOriginal')
        : $t('settings.contentRedaction.showingRedacted')}
    </span>
    <div class="redaction-bar-actions">
      {#if canViewOriginal}
        <button
          type="button"
          class="redaction-link-btn"
          on:click={() => dispatch('rescan')}
          title={$t('settings.contentRedaction.rescanTooltip')}
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
{:else if canViewOriginal}
  <div class="redaction-footer">
    <span class="redaction-status">{$t('settings.contentRedaction.notRedacted')}</span>
    <button
      type="button"
      class="redaction-link-btn"
      on:click={() => dispatch('rescan')}
      title={$t('settings.contentRedaction.rescanTooltip')}
    >
      {$t('settings.contentRedaction.runRedaction')}
    </button>
  </div>
{/if}

<style>
  /* Compact redaction controls placed under the transcript (keeps columns top-aligned). */
  .redaction-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.5rem 0.25rem 0;
    margin-top: 0.5rem;
    border-top: 1px solid var(--border-color);
    flex-wrap: wrap;
  }
  .redaction-status {
    font-size: 0.78rem;
    color: var(--text-muted);
  }
  .redaction-bar-actions {
    display: flex;
    gap: 0.9rem;
    align-items: center;
  }
  /* Small, unobtrusive link-style button. Hover shifts to the primary-hover color with
     a subtle tinted background (consistent with the app) — no underline. */
  .redaction-link-btn {
    background: none;
    border: none;
    padding: 0.15rem 0.35rem;
    border-radius: 4px;
    font-size: 0.78rem;
    font-weight: 500;
    color: var(--primary-on-surface);
    cursor: pointer;
    white-space: nowrap;
    transition:
      color 0.12s ease,
      background-color 0.12s ease;
  }
  .redaction-link-btn:hover {
    color: var(--primary-hover);
    background-color: rgba(var(--primary-color-rgb), 0.1);
  }
  .redaction-link-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .redaction-link-btn:disabled {
    opacity: 0.6;
    cursor: wait;
  }
</style>
