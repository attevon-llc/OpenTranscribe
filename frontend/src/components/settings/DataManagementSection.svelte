<!--
  DataManagementSection.svelte — the "Data & Storage" Settings entry.

  One tab per topic of stored data (file retention, media cache, database backups, data integrity,
  the search index, speaker embeddings), chosen by `dataManagementTabs` from role and capabilities.
  These were five System rows; the old ids (`retention`, `backup`, `data-integrity`,
  `search-indexing`, `embedding-migration`) still open their tab (see `sectionAliases`).

  Each panel loads when its tab is first opened and then stays mounted (`hidden`), so unsaved edits
  survive a tab switch. Panels that already track their own dirty state under their legacy id
  (`retention`, `backup`) keep doing so; the cache panel only dispatches `dirty`, which this shell
  reports. Backups are super_admin: an admin sees the tab locked, not missing.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import LazySection from './LazySection.svelte';
  import { sectionLoaders } from '$lib/settings/sectionLoaders';
  import {
    dataManagementTabs,
    resolveDataTab,
    type DataTabId,
  } from '$lib/settings/dataManagementTabs';

  /** Deep-link support: the legacy section ids open their tab. */
  export let initialTab: DataTabId = 'data-retention';
  export let isAdmin = false;
  export let isSuperAdmin = false;
  export let retentionCap = true;
  export let backupCap = true;
  export let integrityCap = true;
  export let searchCap = true;
  export let embeddingCap = true;

  $: specs = dataManagementTabs({
    isAdmin,
    isSuperAdmin,
    retentionCap,
    backupCap,
    integrityCap,
    searchCap,
    embeddingCap,
  });

  // string, not the id union: <Tabs bind:activeId> is string-typed.
  let activeId: string = initialTab;
  let seenInitial = initialTab;
  $: if (initialTab !== seenInitial) {
    seenInitial = initialTab;
    activeId = initialTab;
  }
  // A tab the role cannot open (or that vanished on demotion) is never left active.
  $: {
    const next = resolveDataTab(activeId as DataTabId, specs);
    if (next && next !== activeId) activeId = next;
  }

  const visited: Record<string, boolean> = {};
  $: if (activeId) visited[activeId] = true;

  let cacheDirty = false;
  $: settingsModalStore.setDirty('data-management', cacheDirty);

  const labelKey: Record<DataTabId, string> = {
    'data-retention': 'settings.retention.title',
    'data-cache': 'settings.cache.title',
    'data-backup': 'settings.backup.title',
    'data-integrity': 'settings.dataIntegrity.title',
    'data-search': 'settings.searchIndexing.title',
    'data-embeddings': 'settings.embeddingMigration.title',
  };

  $: tabItems = specs.map<TabItem>((spec) => {
    const label = $t(labelKey[spec.id]);
    if (spec.locked) {
      return {
        id: spec.id,
        label,
        disabled: true,
        badge: '🔒',
        title: $t('settings.nav.requiresSuperAdmin'),
      };
    }
    return { id: spec.id, label, ...(spec.id === 'data-cache' && cacheDirty ? { badge: '●' } : {}) };
  });

  $: activeSpec = specs.find((s) => s.id === activeId);
  const open = (id: DataTabId) => specs.some((s) => s.id === id && !s.locked);
</script>

<div class="data-management-section" data-testid="data-management-section">
  {#if tabItems.length > 1}
    <Tabs sticky wrap tabs={tabItems} bind:activeId ariaLabel={$t('settings.dataManagement.tabs.ariaLabel')} />
  {/if}

  {#if activeSpec?.locked}
    <EmptyState
      icon="🔒"
      title={$t('settings.permission.superAdminTitle')}
      description={$t('settings.permission.superAdminMessage')}
    />
  {/if}

  {#if open('data-retention')}
    <div id="tabpanel-data-retention" role="tabpanel" aria-labelledby="tab-data-retention" hidden={activeId !== 'data-retention'}>
      {#if visited['data-retention']}
        <LazySection load={sectionLoaders.retention} let:Component><Component /></LazySection>
      {/if}
    </div>
  {/if}

  {#if open('data-cache')}
    <div id="tabpanel-data-cache" role="tabpanel" aria-labelledby="tab-data-cache" hidden={activeId !== 'data-cache'}>
      {#if visited['data-cache']}
        <LazySection load={sectionLoaders.cache} let:Component>
          <Component on:dirty={(e: CustomEvent<boolean>) => (cacheDirty = e.detail)} />
        </LazySection>
      {/if}
    </div>
  {/if}

  {#if open('data-backup')}
    <div id="tabpanel-data-backup" role="tabpanel" aria-labelledby="tab-data-backup" hidden={activeId !== 'data-backup'}>
      {#if visited['data-backup']}
        <LazySection load={sectionLoaders.backup} let:Component><Component /></LazySection>
      {/if}
    </div>
  {/if}

  {#if open('data-integrity')}
    <div id="tabpanel-data-integrity" role="tabpanel" aria-labelledby="tab-data-integrity" hidden={activeId !== 'data-integrity'}>
      {#if visited['data-integrity']}
        <LazySection load={sectionLoaders.dataIntegrity} let:Component><Component /></LazySection>
      {/if}
    </div>
  {/if}

  {#if open('data-search')}
    <div id="tabpanel-data-search" role="tabpanel" aria-labelledby="tab-data-search" hidden={activeId !== 'data-search'}>
      {#if visited['data-search']}
        <h3 class="tab-heading">{$t('settings.searchIndexing.title')}</h3>
        <p class="tab-description">{$t('settings.searchIndexing.description')}</p>
        <LazySection load={sectionLoaders.searchIndexing} variant="dashboard" let:Component><Component /></LazySection>
      {/if}
    </div>
  {/if}

  {#if open('data-embeddings')}
    <div id="tabpanel-data-embeddings" role="tabpanel" aria-labelledby="tab-data-embeddings" hidden={activeId !== 'data-embeddings'}>
      {#if visited['data-embeddings']}
        <LazySection load={sectionLoaders.embeddingMigration} variant="dashboard" let:Component>
          <Component locked={!isSuperAdmin} />
        </LazySection>
        <LazySection load={sectionLoaders.embeddingConsistency} variant="chips" let:Component>
          <Component />
        </LazySection>
      {/if}
    </div>
  {/if}
</div>

<style>
  .tab-heading {
    margin: 0;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .tab-description {
    margin-top: 0.25rem;
    max-width: 800px;
    margin-bottom: 1rem;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }
</style>
