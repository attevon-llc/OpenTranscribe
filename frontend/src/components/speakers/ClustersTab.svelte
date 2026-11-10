<script lang="ts">
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t, locale } from '$stores/locale';
  import { formatDateTime, formatRelativeTime } from '$lib/utils/formatting';
  import SpeakerClusterCard from '$components/speakers/SpeakerClusterCard.svelte';
  import ClusterMemberList from '$components/speakers/ClusterMemberList.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ListRowSkeleton from '$components/ui/ListRowSkeleton.svelte';
  import type {
    SpeakerCluster,
    SpeakerClusterMember
  } from '$lib/types/speakerCluster';

  export let clusterSearch: string;
  export let reclustering = false;
  export let lastClusteredAt: string | null = null;
  export let clusteringProgress: { step: number; total_steps: number; message: string; progress: number } | null = null;
  export let loadingClusters = false;
  export let clusters: SpeakerCluster[] = [];
  export let identifiedClusters: SpeakerCluster[] = [];
  export let unidentifiedClusters: SpeakerCluster[] = [];
  export let labeledCount = 0;
  export let unlabeledCount = 0;
  export let identifiedCollapsed = false;
  export let unidentifiedCollapsed = false;
  export let mergeMode = false;
  export let mergeSourceUuid: string | null = null;
  export let expandedCluster: string | null = null;
  export let clusterMembers: Record<string, SpeakerClusterMember[]> = {};
  export let splitMode = false;
  export let splitTargetUuid: string | null = null;
  export let splitSelectedMembers: Set<string> = new Set();
  export let unassignMode = false;
  export let unassignTargetUuid: string | null = null;
  export let unassignSelectedMembers: Set<string> = new Set();
  export let unassignBlacklist = true;
  export let clusterPage = 1;
  export let clusterPages = 0;
  export let reviewCount = 0;

  const dispatch = createEventDispatcher();
</script>

<div class="tab-content">
  <div class="toolbar">
    <div class="search-input-wrapper">
      <input
        type="text"
        class="search-input"
        placeholder={$t('speakers.searchPlaceholder')}
        bind:value={clusterSearch}
        on:input={() => dispatch('search')}
      />
      {#if clusterSearch}
        <button
          class="search-clear-btn"
          on:click={() => { clusterSearch = ''; dispatch('search'); }}
          title={$t('common.clear')}
          aria-label={$t('common.clear')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="15" y1="9" x2="9" y2="15"></line>
            <line x1="9" y1="9" x2="15" y2="15"></line>
          </svg>
        </button>
      {/if}
    </div>
    <button class="btn-recluster" on:click={() => dispatch('recluster')} disabled={reclustering} title={$t('speakers.tooltip.recluster')}>
      {reclustering ? $t('speakers.clusters.reclustering') : $t('speakers.clusters.recluster')}
    </button>
    {#if lastClusteredAt}
      <span class="last-clustered-chip" title={formatDateTime(lastClusteredAt, '', $locale)}>
        {$t('speakers.clusters.lastRun')}: {formatRelativeTime(new Date(lastClusteredAt), Date.now(), $t)}
      </span>
    {/if}
  </div>

  {#if reclustering && clusteringProgress}
    <div class="clustering-progress">
      {#if clusteringProgress.total_steps === 0}
        <!-- Queued state: waiting for GPU -->
        <div class="queued-pulse">
          <ProgressBar percent={100} ariaLabel={$t('speakers.clusters.reclustering')} />
        </div>
        <span class="progress-text">{clusteringProgress.message}</span>
      {:else}
        <ProgressBar
          percent={clusteringProgress.progress * 100}
          ariaLabel={$t('speakers.clusters.reclustering')}
        />
        <span class="progress-text">
          {clusteringProgress.message} ({Math.round(clusteringProgress.progress * 100)}%)
        </span>
      {/if}
    </div>
  {/if}

  {#if loadingClusters}
    <ListRowSkeleton count={6} />
  {:else if clusters.length === 0}
    <EmptyState
      title={$t('speakers.clusters.emptyTitle')}
      description={reviewCount > 0
        ? $t('speakers.clusters.emptyReviewDesc', { count: reviewCount })
        : $t('speakers.clusters.emptyDesc')}
      padding="60px 20px"
    >
      <svelte:fragment slot="icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <circle cx="12" cy="12" r="10" /><path d="M8 12h8" /><path d="M12 8v8" />
        </svg>
      </svelte:fragment>
      {#if reviewCount > 0}
        <button type="button" class="btn btn-secondary" on:click={() => dispatch('openReview')}>
          {$t('speakers.clusters.openReview')}
        </button>
      {/if}
    </EmptyState>
  {:else}
    {#if mergeMode}
      <div class="merge-banner">
        <span>{$t('speakers.merge.selectTargetWithName', { name: clusters.find(c => c.uuid === mergeSourceUuid)?.label || $t('speakers.cluster.unlabeled') })}</span>
        <button class="btn-cancel-merge" on:click={() => dispatch('cancelMerge')}>{$t('modal.cancel')}</button>
      </div>
    {/if}

  {#snippet clusterItem(cluster: SpeakerCluster)}
        <div class:merge-source-highlight={mergeMode && mergeSourceUuid === cluster.uuid}>
          <SpeakerClusterCard
            {cluster}
            expanded={expandedCluster === cluster.uuid}

            unassignActive={unassignMode && unassignTargetUuid === cluster.uuid}
            unassignSelectedCount={unassignMode && unassignTargetUuid === cluster.uuid ? unassignSelectedMembers.size : 0}
            unassignTotalCount={clusterMembers[cluster.uuid]?.length ?? cluster.member_count}
            {unassignBlacklist}
            on:expand
            on:update
            on:promote
            on:delete
            on:merge
            on:split
            on:unassign
            on:cancelUnassign
            on:confirmUnassign
            on:toggleBlacklist
          >
            <div slot="members">
              {#if clusterMembers[cluster.uuid]}
                <ClusterMemberList
                  members={clusterMembers[cluster.uuid]}
                  {cluster}
                  {splitMode}
                  {splitTargetUuid}
                  {splitSelectedMembers}
                  {unassignMode}
                  {unassignTargetUuid}
                  {unassignSelectedMembers}
                  on:preview
                  on:prefetch
                  on:toggleSplitMember
                  on:toggleUnassignMember
                  on:cancelSplit
                  on:confirmSplit
                  on:outlierAnalysisComplete
                />
              {:else}
                <div class="loading-members">{$t('speakers.members.loading')}</div>
              {/if}
            </div>
          </SpeakerClusterCard>
        </div>
  {/snippet}

    <div class="cluster-list">
      {#if labeledCount > 0}
        <button class="section-heading-btn identified" aria-expanded={!identifiedCollapsed} on:click={() => dispatch('toggleSection', 'identified')} title={identifiedCollapsed ? $t('speakers.tooltip.expandSection') : $t('speakers.tooltip.collapseSection')}>
          <span class="section-chevron" class:collapsed={identifiedCollapsed} aria-hidden="true"><MenuIcon name={identifiedCollapsed ? 'chevron-right' : 'chevron-down'} size="xs" /></span>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
          {$t('speakers.cluster.identifiedSpeakers')} ({labeledCount})
        </button>
        {#if !identifiedCollapsed}
          {#if identifiedClusters.length > 0}
            {#each identifiedClusters as cluster (cluster.uuid)}
              {@render clusterItem(cluster)}
            {/each}
          {:else}
            <div class="section-empty-note">{$t('speakers.section.allOnOtherPages')}</div>
          {/if}
        {/if}
      {/if}

      {#if unlabeledCount > 0}
        <button class="section-heading-btn unidentified" aria-expanded={!unidentifiedCollapsed} on:click={() => dispatch('toggleSection', 'unidentified')} title={unidentifiedCollapsed ? $t('speakers.tooltip.expandSection') : $t('speakers.tooltip.collapseSection')}>
          <span class="section-chevron" class:collapsed={unidentifiedCollapsed} aria-hidden="true"><MenuIcon name={unidentifiedCollapsed ? 'chevron-right' : 'chevron-down'} size="xs" /></span>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          {$t('speakers.cluster.unidentifiedClusters')} ({unlabeledCount})
        </button>
        {#if !unidentifiedCollapsed}
          {#if unidentifiedClusters.length > 0}
            {#each unidentifiedClusters as cluster (cluster.uuid)}
              {@render clusterItem(cluster)}
            {/each}
          {:else}
            <div class="section-empty-note">{$t('speakers.section.allOnOtherPages')}</div>
          {/if}
        {/if}
      {/if}
    </div>

    {#if clusterPages > 1}
      <div class="pagination">
        <button disabled={clusterPage <= 1} on:click={() => dispatch('prevPage')}>
          {$t('speakers.pagination.prev')}
        </button>
        <span>{$t('speakers.pagination.pageOf', { page: clusterPage, pages: clusterPages })}</span>
        <button disabled={clusterPage >= clusterPages} on:click={() => dispatch('nextPage')}>
          {$t('speakers.pagination.next')}
        </button>
      </div>
    {/if}
  {/if}
</div>

<style>
  .toolbar {
    display: flex;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
    align-items: center;
    flex-wrap: wrap;
  }

  .search-input-wrapper {
    position: relative;
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
  }

  .search-clear-btn {
    position: absolute;
    right: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary);
    padding: var(--space-1);
    border-radius: var(--radius-round);
    transition: color var(--duration-fast), background var(--duration-fast);
  }

  .search-clear-btn:hover {
    color: var(--text-color);
    background: var(--hover-color);
  }

  .search-input {
    width: 100%;
    min-width: 0;
    padding: var(--space-2) var(--space-8) var(--space-2) var(--space-3);
    font-size: var(--font-size-md);
    box-sizing: border-box;
  }

  .search-input::placeholder {
    color: var(--text-secondary);
  }

  .btn-recluster {
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-lg);
    border: none;
    background: var(--primary-color);
    color: var(--text-on-solid);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    white-space: nowrap;
    box-shadow: var(--shadow-primary);
    transition: all var(--duration-base) ease;
  }

  .btn-recluster:hover:not(:disabled) {
    background: var(--primary-color);
    transform: scale(1.02);
    box-shadow: var(--shadow-primary-hover);
  }

  .btn-recluster:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-recluster:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .last-clustered-chip {
    font-size: var(--font-size-2xs);
    /* 11px on the hover tint — --text-secondary measures 4.34:1 here, under AA. */
    color: var(--text-on-tint);
    background: var(--hover-color);
    padding: 1px 7px;
    border-radius: var(--radius-xl);
    white-space: nowrap;
    align-self: center;
  }

  .clustering-progress {
    margin-top: var(--space-3);
  }

  .queued-pulse {
    opacity: 0.4;
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 0.3; }
    50% { opacity: 0.6; }
  }

  .progress-text {
    display: block;
    margin-top: var(--space-1);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .cluster-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .section-heading-btn {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    margin: var(--space-4) 0 var(--space-1);
    padding: var(--space-1) var(--space-1);
    border: none;
    background: none;
    cursor: pointer;
    width: 100%;
    text-align: left;
    border-radius: var(--radius-sm);
    transition: background var(--duration-fast) ease;
    box-shadow: none;
  }

  .section-heading-btn:hover {
    background: var(--hover-color);
    transform: none;
    box-shadow: none;
  }

  .section-heading-btn:first-child {
    margin-top: 0;
  }

  .section-heading-btn.identified {
    color: var(--success-text);
  }

  .section-heading-btn.unidentified {
    color: var(--text-secondary);
  }

  .section-heading-btn svg {
    opacity: 0.7;
  }

  .section-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    flex-shrink: 0;
    transition: transform var(--duration-fast) ease;
  }

  .section-empty-note {
    padding: var(--space-3) var(--space-6);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    font-style: italic;
  }

  .loading-members {
    padding: var(--space-3);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .pagination {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-4);
    margin-top: var(--space-4);
    font-size: var(--font-size-md);
    color: var(--text-secondary);
  }

  .pagination button {
    padding: var(--space-1-5) var(--space-3-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--card-background);
    color: var(--text-color);
    cursor: pointer;
    box-shadow: none;
    font-size: var(--font-size-md);
  }

  .pagination button:hover:not(:disabled) {
    background: var(--hover-color);
    transform: none;
    box-shadow: none;
  }

  .pagination button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .merge-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-4);
    background: color-mix(in srgb, var(--primary-color) 10%, transparent);
    border: 1px solid var(--primary-color);
    border-radius: var(--radius-md);
    margin-bottom: var(--space-3);
    font-size: var(--font-size-md);
    color: var(--primary-on-surface);
  }

  .merge-banner span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .btn-cancel-merge {
    padding: var(--space-1) var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--card-background);
    color: var(--text-color);
    cursor: pointer;
    font-size: var(--font-size-sm);
    box-shadow: none;
  }

  .btn-cancel-merge:hover {
    background: var(--hover-color);
    transform: none;
    box-shadow: none;
  }

  .merge-source-highlight {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: var(--radius-lg);
  }

  @media (max-width: 768px) {
    .toolbar {
      flex-wrap: wrap;
      gap: var(--space-2);
    }

    .search-input {
      flex-basis: 100%;
      min-width: 0;
    }

    .btn-recluster {
      flex: 1;
      min-width: 0;
      padding: var(--space-2) var(--space-3);
      font-size: var(--font-size-sm);
    }

    .last-clustered-chip {
      font-size: 0.7rem;
      flex-basis: 100%;
      text-align: center;
    }

    .merge-banner {
      flex-wrap: wrap;
      padding: var(--space-2) var(--space-3);
      font-size: var(--font-size-sm);
    }

    .merge-banner span {
      white-space: normal;
      word-break: break-word;
    }

    .section-heading-btn {
      font-size: var(--font-size-xs);
      padding: var(--space-1) var(--space-0-5);
    }

    .pagination {
      gap: var(--space-2);
      font-size: var(--font-size-sm);
    }

    .pagination button {
      padding: var(--space-1-5) var(--space-2-5);
      font-size: var(--font-size-sm);
    }
  }
</style>
