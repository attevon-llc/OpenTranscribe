<script lang="ts">
  /**
   * Local LLM server detection (issue #644) as a disclosure.
   *
   * Collapsed by default with the result in the summary line, so it stays out of the way once a
   * provider is chosen; the parent decides when it is shown at all and when it starts expanded.
   * "Use this" and "Rescan" are events: the parent owns the form state (and the ordering trap
   * documented on its `useDiscoveredEndpoint`).
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { LocalEndpointStatus } from '$lib/api/llmSettings';
  import ExpandableSection from '$components/ui/ExpandableSection.svelte';
  import Spinner from '$components/ui/Spinner.svelte';

  export let endpoints: LocalEndpointStatus[] = [];
  export let loading = false;
  export let privateAllowed = true;
  export let disabled = false;
  export let expanded = false;

  const dispatch = createEventDispatcher<{
    use: LocalEndpointStatus;
    rescan: void;
    toggle: boolean;
  }>();

  $: runningCount = endpoints.filter((e) => e.reachable).length;
  $: summary = loading
    ? $t('llm.discovery.summaryChecking')
    : runningCount > 0
      ? $t('llm.discovery.summaryDetected', { count: runningCount })
      : $t('llm.discovery.summaryNone');
</script>

<div class="discovery-panel form-group">
  <ExpandableSection
    title={$t('llm.discovery.title')}
    bind:expanded
    on:toggle={(e) => dispatch('toggle', e.detail)}
  >
    <svelte:fragment slot="header-extra">
      <span class="discovery-summary" class:discovery-summary-active={runningCount > 0 && !loading}>
        {summary}
      </span>
    </svelte:fragment>

    <div class="discovery-toolbar">
      <button
        type="button"
        class="discovery-rescan-btn"
        on:click={() => dispatch('rescan')}
        disabled={loading}
        title={$t('llm.discovery.rescanTooltip')}
      >
        {#if loading}
          <Spinner size="small" />
        {:else}
          {$t('llm.discovery.rescan')}
        {/if}
      </button>
    </div>

    {#if endpoints.length > 0}
      <ul class="discovery-list">
        {#each endpoints as endpoint (endpoint.id)}
          <li class="discovery-row" class:discovery-row-unreachable={!endpoint.reachable}>
            <span
              class="discovery-status-dot"
              class:discovery-status-dot-reachable={endpoint.reachable}
              aria-hidden="true"
            ></span>
            <div class="discovery-row-body">
              <div class="discovery-row-label">{endpoint.label}</div>
              {#if endpoint.reachable}
                <div class="discovery-row-detail">
                  <code>{endpoint.base_url}</code>
                  {#if endpoint.models.length > 0}
                    <span class="discovery-row-models">{endpoint.models.join(', ')}</span>
                  {/if}
                </div>
              {:else}
                <div class="discovery-row-detail discovery-row-detail-muted">
                  {$t('llm.discovery.notRunning')}
                  <code>{endpoint.start_command}</code>
                </div>
              {/if}
            </div>
            {#if endpoint.reachable}
              <button
                type="button"
                class="btn btn-secondary discovery-use-btn"
                on:click={() => dispatch('use', endpoint)}
                {disabled}
              >
                {$t('llm.discovery.useThis')}
              </button>
            {/if}
          </li>
        {/each}
      </ul>

      {#if !privateAllowed && endpoints.some((e) => e.reachable)}
        <p class="discovery-warning">{$t('llm.discovery.privateEndpointsDisabledWarning')}</p>
      {/if}
    {/if}
  </ExpandableSection>
</div>

<style>
  .discovery-panel {
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--card-bg);
    padding: 0 1rem;
    margin-bottom: 1.5rem;
  }

  /* The disclosure draws its own rule under the header; the panel border replaces it. */
  .discovery-panel :global(.expandable) {
    border-bottom: none;
  }

  .discovery-summary {
    font-size: 0.8125rem;
    font-weight: 400;
    color: var(--text-muted);
  }

  .discovery-summary-active {
    color: var(--text-color);
  }

  .discovery-toolbar {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 0.5rem;
  }

  .discovery-rescan-btn {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 0.2rem 0.6rem;
    font-size: 0.75rem;
    color: var(--text-color);
    cursor: pointer;
    transition:
      background-color 0.2s,
      border-color 0.2s;
  }

  .discovery-rescan-btn:hover:not(:disabled) {
    background: var(--hover-color);
    border-color: var(--primary-color);
  }

  .discovery-rescan-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .discovery-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .discovery-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.4rem 0;
  }

  .discovery-row-unreachable {
    opacity: 0.7;
  }

  .discovery-status-dot {
    flex-shrink: 0;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-muted);
  }

  .discovery-status-dot-reachable {
    background: var(--success-color);
  }

  .discovery-row-body {
    flex: 1;
    min-width: 0;
  }

  .discovery-row-label {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .discovery-row-detail {
    font-size: 0.75rem;
    color: var(--text-muted);
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: baseline;
    word-break: break-all;
  }

  .discovery-row-detail code {
    font-size: 0.75rem;
  }

  .discovery-row-detail-muted {
    font-style: italic;
  }

  .discovery-row-models {
    color: var(--text-color);
  }

  .discovery-use-btn {
    flex-shrink: 0;
    padding: 0.3rem 0.75rem;
    font-size: 0.75rem;
  }

  .discovery-warning {
    margin: 0.75rem 0 0;
    font-size: 0.75rem;
    color: var(--warning-color);
  }
</style>
