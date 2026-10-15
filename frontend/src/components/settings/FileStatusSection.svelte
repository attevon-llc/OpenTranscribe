<!--
  FileStatusSection.svelte — the "File status" Settings entry.

  Two tabs: Status (what is happening to your files; everyone) and Recovery (find and restart stuck
  work; admins, from the old Task Health Monitor row). Tabs come from `fileStatusTabs`; a user with
  one tab gets no strip. `admin-task-health` still opens the Recovery tab (see `sectionAliases`).

  Each panel loads when its tab is first opened and then stays mounted (`hidden`). The Recovery
  panel's `requestConfirm` is forwarded so the modal's confirmation dialog handles it.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import LazySection from './LazySection.svelte';
  import type { ConfirmRequest } from './AdminTaskHealthPanel.svelte';
  import { sectionLoaders } from '$lib/settings/sectionLoaders';
  import { fileStatusTabs, resolveStatusTab, type StatusTabId } from '$lib/settings/fileStatusTabs';

  /** Deep-link support: `admin-task-health` opens the Recovery tab. */
  export let initialTab: StatusTabId = 'status-files';
  export let isAdmin = false;
  export let taskHealthCap = true;

  const dispatch = createEventDispatcher<{ requestConfirm: ConfirmRequest }>();

  $: specs = fileStatusTabs({ isAdmin, taskHealthCap });

  // string, not the id union: <Tabs bind:activeId> is string-typed.
  let activeId: string = initialTab;
  let seenInitial = initialTab;
  $: if (initialTab !== seenInitial) {
    seenInitial = initialTab;
    activeId = initialTab;
  }
  $: {
    const next = resolveStatusTab(activeId as StatusTabId, specs);
    if (next && next !== activeId) activeId = next;
  }

  const visited: Record<string, boolean> = {};
  $: if (activeId) visited[activeId] = true;

  const labelKey: Record<StatusTabId, string> = {
    'status-files': 'settings.fileStatus.tabs.status',
    'status-recovery': 'settings.fileStatus.tabs.recovery',
  };

  $: tabItems = specs.map<TabItem>((spec) => ({ id: spec.id, label: $t(labelKey[spec.id]) }));
  $: recoveryOpen = specs.some((s) => s.id === 'status-recovery' && !s.locked);
</script>

<div class="file-status-section" data-testid="file-status-section">
  {#if tabItems.length > 1}
    <Tabs sticky wrap tabs={tabItems} bind:activeId ariaLabel={$t('settings.fileStatus.tabs.ariaLabel')} />
  {/if}

  <div id="tabpanel-status-files" role="tabpanel" aria-labelledby="tab-status-files" hidden={activeId !== 'status-files'}>
    {#if visited['status-files']}
      <LazySection load={sectionLoaders.fileStatus} let:Component><Component /></LazySection>
    {/if}
  </div>

  {#if recoveryOpen}
    <div id="tabpanel-status-recovery" role="tabpanel" aria-labelledby="tab-status-recovery" hidden={activeId !== 'status-recovery'}>
      {#if visited['status-recovery']}
        <LazySection load={sectionLoaders.taskHealth} variant="dashboard" let:Component>
          <Component on:requestConfirm={(e: CustomEvent<ConfirmRequest>) => dispatch('requestConfirm', e.detail)} />
        </LazySection>
      {/if}
    </div>
  {/if}
</div>
