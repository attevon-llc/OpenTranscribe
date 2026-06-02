<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import SpeakerClusterCard from '$components/speakers/SpeakerClusterCard.svelte';
  import ClusterMemberList from '$components/speakers/ClusterMemberList.svelte';
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

  const dispatch = createEventDispatcher();

  function formatRelativeTime(isoDate: string): string {
    const diff = Date.now() - new Date(isoDate).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return $t('speakers.clusters.justNow');
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }
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
      <span class="last-clustered-chip" title={new Date(lastClusteredAt).toLocaleString()}>
        {$t('speakers.clusters.lastRun')}: {formatRelativeTime(lastClusteredAt)}
      </span>
    {/if}
  </div>

  {#if reclustering && clusteringProgress}
    <div class="clustering-progress">
      {#if clusteringProgress.total_steps === 0}
        <!-- Queued state: waiting for GPU -->
        <div class="progress-bar">
          <div class="progress-fill queued-pulse" style="width: 100%"></div>
        </div>
        <span class="progress-text">{clusteringProgress.message}</span>
      {:else}
        <div class="progress-bar">
          <div class="progress-fill" style="width: {clusteringProgress.progress * 100}%"></div>
        </div>
        <span class="progress-text">
          {clusteringProgress.message} ({Math.round(clusteringProgress.progress * 100)}%)
        </span>
      {/if}
    </div>
  {/if}

  {#if loadingClusters}
    <ListRowSkeleton count={6} />
  {:else if clusters.length === 0}
    <EmptyState title={$t('speakers.clusters.emptyTitle')} description={$t('speakers.clusters.emptyDesc')} padding="60px 20px">
      <svelte:fragment slot="icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <circle cx="12" cy="12" r="10" /><path d="M8 12h8" /><path d="M12 8v8" />
        </svg>
      </svelte:fragment>
    </EmptyState>
  {:else}
    {#if mergeMode}
      <div class="merge-banner">
        <span>{$t('speakers.merge.selectTargetWithName', { name: clusters.find(c => c.uuid === mergeSourceUuid)?.label || $t('speakers.cluster.unlabeled') })}</span>
        <button class="btn-cancel-merge" on:click={() => dispatch('cancelMerge')}>{$t('modal.cancel')}</button>
      </div>
    {/if}

    <div class="cluster-list">
      {#if labeledCount > 0}
        <button class="section-heading-btn identified" on:click={() => dispatch('toggleSection', 'identified')} title={identifiedCollapsed ? $t('speakers.tooltip.expandSection') : $t('speakers.tooltip.collapseSection')}>
          <span class="section-chevron" class:collapsed={identifiedCollapsed}>{identifiedCollapsed ? '▸' : '▾'}</span>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
          {$t('speakers.cluster.identifiedSpeakers')} ({labeledCount})
        </button>
        {#if !identifiedCollapsed}
          {#if identifiedClusters.length > 0}
            {#each identifiedClusters as cluster (cluster.uuid)}
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
            {/each}
          {:else}
            <div class="section-empty-note">{$t('speakers.section.allOnOtherPages')}</div>
          {/if}
        {/if}
      {/if}

      {#if unlabeledCount > 0}
        <button class="section-heading-btn unidentified" on:click={() => dispatch('toggleSection', 'unidentified')} title={unidentifiedCollapsed ? $t('speakers.tooltip.expandSection') : $t('speakers.tooltip.collapseSection')}>
          <span class="section-chevron" class:collapsed={unidentifiedCollapsed}>{unidentifiedCollapsed ? '▸' : '▾'}</span>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          {$t('speakers.cluster.unidentifiedClusters')} ({unlabeledCount})
        </button>
        {#if !unidentifiedCollapsed}
          {#if unidentifiedClusters.length > 0}
            {#each unidentifiedClusters as cluster (cluster.uuid)}
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
    gap: 12px;
    margin-bottom: 16px;
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
    padding: 4px;
    border-radius: 50%;
    transition: color 0.15s, background 0.15s;
  }

  .search-clear-btn:hover {
    color: var(--text-color);
    background: var(--hover-color, rgba(0, 0, 0, 0.05));
  }

  .search-input {
    width: 100%;
    min-width: 0;
    padding: 8px 32px 8px 12px;
    border: 1px solid var(--input-border);
    border-radius: 6px;
    background: var(--input-background);
    color: var(--text-color);
    font-size: 14px;
    box-sizing: border-box;
  }

  .search-input::placeholder {
    color: var(--text-secondary);
  }

  .search-input:focus {
    outline: none;
    border-color: var(--input-focus-border);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-color, #3b82f6) 10%, transparent);
  }

  .btn-recluster {
    padding: 8px 16px;
    border-radius: 8px;
    border: none;
    background: #3b82f6;
    color: white;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
    transition: all 0.2s ease;
  }

  .btn-recluster:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .btn-recluster:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-recluster:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .last-clustered-chip {
    font-size: 11px;
    color: var(--text-secondary);
    background: var(--hover-color);
    padding: 1px 7px;
    border-radius: 10px;
    white-space: nowrap;
    align-self: center;
  }

  .clustering-progress {
    margin-top: 12px;
  }

  .progress-bar {
    height: 6px;
    background: var(--hover-color);
    border-radius: 3px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background: var(--primary-color);
    border-radius: 3px;
    transition: width 0.3s ease;
  }

  .progress-fill.queued-pulse {
    opacity: 0.4;
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 0.3; }
    50% { opacity: 0.6; }
  }

  .progress-text {
    display: block;
    margin-top: 4px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .cluster-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .section-heading-btn {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary, #6b7280);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 16px 0 4px;
    padding: 4px 4px;
    border: none;
    background: none;
    cursor: pointer;
    width: 100%;
    text-align: left;
    border-radius: 4px;
    transition: background 0.15s ease;
    box-shadow: none;
  }

  .section-heading-btn:hover {
    background: var(--hover-color, #f9fafb);
    transform: none;
    box-shadow: none;
  }

  .section-heading-btn:first-child {
    margin-top: 0;
  }

  .section-heading-btn.identified {
    color: var(--success-color, #059669);
  }

  .section-heading-btn.unidentified {
    color: var(--text-secondary, #6b7280);
  }

  .section-heading-btn svg {
    opacity: 0.7;
  }

  .section-chevron {
    font-size: 12px;
    width: 16px;
    flex-shrink: 0;
    transition: transform 0.15s ease;
  }

  .section-empty-note {
    padding: 12px 24px;
    font-size: 13px;
    color: var(--text-secondary, #6b7280);
    font-style: italic;
  }

  .loading-members {
    padding: 12px;
    color: var(--text-secondary);
    font-size: 13px;
  }

  .pagination {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 16px;
    margin-top: 16px;
    font-size: 14px;
    color: var(--text-secondary);
  }

  .pagination button {
    padding: 6px 14px;
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 6px;
    background: var(--card-background, #fff);
    color: var(--text-color, #111827);
    cursor: pointer;
    box-shadow: none;
    font-size: 14px;
  }

  .pagination button:hover:not(:disabled) {
    background: var(--hover-color, #f3f4f6);
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
    gap: 8px;
    padding: 8px 16px;
    background: color-mix(in srgb, var(--primary-color, #3b82f6) 10%, transparent);
    border: 1px solid var(--primary-color, #3b82f6);
    border-radius: 6px;
    margin-bottom: 12px;
    font-size: 14px;
    color: var(--primary-color, #3b82f6);
  }

  .merge-banner span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .btn-cancel-merge {
    padding: 4px 12px;
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 6px;
    background: var(--card-background, #fff);
    color: var(--text-color, #111827);
    cursor: pointer;
    font-size: 13px;
    box-shadow: none;
  }

  .btn-cancel-merge:hover {
    background: var(--hover-color, #f3f4f6);
    transform: none;
    box-shadow: none;
  }

  .merge-source-highlight {
    outline: 2px solid var(--primary-color, #3b82f6);
    outline-offset: 2px;
    border-radius: 8px;
  }

  @media (max-width: 768px) {
    .toolbar {
      flex-wrap: wrap;
      gap: 8px;
    }

    .search-input {
      flex-basis: 100%;
      min-width: 0;
    }

    .btn-recluster {
      flex: 1;
      min-width: 0;
      padding: 8px 12px;
      font-size: 13px;
    }

    .last-clustered-chip {
      font-size: 0.7rem;
      flex-basis: 100%;
      text-align: center;
    }

    .merge-banner {
      flex-wrap: wrap;
      padding: 8px 12px;
      font-size: 13px;
    }

    .merge-banner span {
      white-space: normal;
      word-break: break-word;
    }

    .section-heading-btn {
      font-size: 12px;
      padding: 4px 2px;
    }

    .pagination {
      gap: 8px;
      font-size: 13px;
    }

    .pagination button {
      padding: 6px 10px;
      font-size: 13px;
    }
  }
</style>
