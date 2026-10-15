<!--
  ChatSettingsPanel.svelte — one Settings entry for everything chat.

  Chat used to occupy two sidebar rows ("Chat" and "Chat & RAG"), which made the
  settings list longer and left users guessing which one held the knob they
  wanted. They are now tabs behind a single entry, ordered by who needs them:
  the per-user chat defaults first, platform tuning second and admin-only.

  The Advanced tab is not merely hidden for non-admins — it is never rendered,
  and the tab strip collapses to nothing, so a normal user sees a plain panel.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import Tabs from '$components/ui/Tabs.svelte';
  import ChatSettings from '$components/settings/ChatSettings.svelte';
  import ChatAdminSettings from '$components/settings/ChatAdminSettings.svelte';

  /** Deep-link support: the settings search can target the advanced tab. */
  export let initialTab: 'general' | 'advanced' = 'general';
  export let isAdmin = false;

  // string, not the id union: <Tabs bind:activeId> is string-typed.
  let activeTab: string = initialTab;

  // A non-admin can never sit on a tab that does not exist for them.
  $: if (!isAdmin && activeTab === 'advanced') activeTab = 'general';

  $: tabs = [
    { id: 'general' as const, label: $t('chat.settings.tabs.general') },
    ...(isAdmin ? [{ id: 'advanced' as const, label: $t('chat.settings.tabs.advanced') }] : []),
  ];
</script>

<div class="chat-settings-panel" data-testid="chat-settings-panel">
  {#if tabs.length > 1}
    <Tabs sticky {tabs} bind:activeId={activeTab} ariaLabel={$t('chat.settings.title')} />
  {/if}

  <div class="tab-content">
    {#if activeTab === 'general'}
      <ChatSettings />
    {:else if activeTab === 'advanced' && isAdmin}
      <p class="tab-intro">{$t('chat.settings.tabs.advancedIntro')}</p>
      <ChatAdminSettings />
    {/if}
  </div>
</div>

<style>
  .chat-settings-panel {
    display: flex;
    flex-direction: column;
  }

  .tab-intro {
    margin: 0 0 1.25rem;
    padding: 0.7rem 0.85rem;
    border-left: 3px solid var(--primary-color);
    border-radius: 0 6px 6px 0;
    background-color: rgba(var(--primary-color-rgb), 0.08);
    font-size: 0.82rem;
    line-height: 1.5;
    color: var(--text-secondary);
  }
</style>
