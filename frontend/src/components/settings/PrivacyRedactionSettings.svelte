<!--
  PrivacyRedactionSettings.svelte — one Settings entry for everything redaction.

  Content Redaction (a per-user preference) and Redaction Policy (the admin floor
  that overrides it) used to be two sidebar rows with near-identical names. They
  are tabs behind one entry now; which tabs exist is decided by
  `privacyRedactionTabs` from the user's role, so the panel only renders it.

  Both panels stay mounted once visited and are merely hidden when inactive: each
  holds unsaved edits and reports them to the modal's dirty-state guard, so
  unmounting on a tab switch would silently throw the edits away.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ContentRedactionSettings from '$components/settings/ContentRedactionSettings.svelte';
  import RedactionPolicySettings from '$components/settings/RedactionPolicySettings.svelte';
  import {
    privacyRedactionTabs,
    resolvePrivacyRedactionTab,
    type PrivacyRedactionTabId,
  } from '$lib/settings/privacyRedactionTabs';

  /** Deep-link support: the legacy `redaction-policy` section id opens the policy tab. */
  export let initialTab: PrivacyRedactionTabId = 'personal';
  export let isAdmin = false;
  export let isSuperAdmin = false;
  export let userCap = true;
  export let policyCap = true;

  $: specs = privacyRedactionTabs({ isAdmin, isSuperAdmin, userCap, policyCap });

  // string, not the id union: <Tabs bind:activeId> is string-typed.
  let activeId: string = initialTab;
  let seenInitial = initialTab;
  $: if (initialTab !== seenInitial) {
    seenInitial = initialTab;
    activeId = initialTab;
  }
  // A tab the role cannot open (or that vanished on demotion) is never left active.
  $: {
    const next = resolvePrivacyRedactionTab(activeId as PrivacyRedactionTabId, specs);
    if (next && next !== activeId) activeId = next;
  }

  const visited: Record<string, boolean> = {};
  $: if (activeId) visited[activeId] = true;

  $: tabItems = specs.map<TabItem>((spec) => {
    const label =
      spec.id === 'personal'
        ? $t('settings.contentRedaction.title')
        : $t('settings.redactionPolicy.title');
    return spec.locked
      ? { id: spec.id, label, disabled: true, badge: '🔒', title: $t('settings.nav.requiresSuperAdmin') }
      : { id: spec.id, label };
  });

  $: activeSpec = specs.find((s) => s.id === activeId);
</script>

<div class="privacy-redaction" data-testid="privacy-redaction-panel">
  {#if tabItems.length > 1}
    <div class="tab-strip">
      <Tabs tabs={tabItems} bind:activeId ariaLabel={$t('settings.privacyRedaction.title')} />
    </div>
  {/if}

  {#if activeSpec?.locked}
    <EmptyState
      icon="🔒"
      title={$t('settings.permission.superAdminTitle')}
      description={$t('settings.permission.superAdminMessage')}
    />
  {/if}

  {#if specs.some((s) => s.id === 'personal')}
    <div
      id="tabpanel-personal"
      role="tabpanel"
      aria-labelledby="tab-personal"
      hidden={activeId !== 'personal'}
    >
      {#if visited.personal}<ContentRedactionSettings />{/if}
    </div>
  {/if}

  {#if specs.some((s) => s.id === 'policy' && !s.locked)}
    <div
      id="tabpanel-policy"
      role="tabpanel"
      aria-labelledby="tab-policy"
      hidden={activeId !== 'policy'}
    >
      {#if visited.policy}<RedactionPolicySettings />{/if}
    </div>
  {/if}
</div>

<style>
  .tab-strip {
    margin-bottom: 1.25rem;
  }
</style>
