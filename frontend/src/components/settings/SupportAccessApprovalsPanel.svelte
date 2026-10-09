<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import GrantRequestsPane from '$components/supportAccess/GrantRequestsPane.svelte';
  import type { TenantPerspective } from '$lib/api/supportAccess';

  /**
   * Settings > Account > Support access requests: platform staff must ask before they can open
   * a tenant's content, and this is where the tenant answers. Every user has a personal
   * workspace; an organization admin also answers for their organization. `orgTab` is
   * cosmetic (the shell decides it from the edition and role), the server is the authority.
   *
   * Emits `countchange` whenever a pane (re)loads so the shell, which owns the sidebar badge
   * for BOTH tabs, can re-count.
   */
  export let orgTab = false;

  const dispatch = createEventDispatcher<{ countchange: void }>();

  let active: TenantPerspective = 'workspace';

  $: tabs = [
    { id: 'workspace', label: $t('supportAccess.tabs.personal') },
    ...(orgTab ? [{ id: 'org', label: $t('supportAccess.tabs.organization') }] : []),
  ] as TabItem[];
  // The organization tab can disappear (role change); never stay on a tab that is gone.
  $: if (!orgTab && active === 'org') active = 'workspace';

  function changeTab(event: CustomEvent<string>) {
    active = event.detail === 'org' ? 'org' : 'workspace';
  }
</script>

<div class="panel">
  <p class="description">{$t('settings.supportAccessRequests.description')}</p>
  {#if orgTab}
    <Tabs {tabs} activeId={active} on:change={changeTab} />
  {/if}
  {#key active}
    <GrantRequestsPane perspective={active} on:countchange={() => dispatch('countchange')} />
  {/key}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .description {
    margin: 0;
    font-size: 0.875rem;
    line-height: 1.5;
    color: var(--text-secondary);
  }
</style>
