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
  import ChatSettings from '$components/settings/ChatSettings.svelte';
  import ChatAdminSettings from '$components/settings/ChatAdminSettings.svelte';

  /** Deep-link support: the settings search can target the advanced tab. */
  export let initialTab: 'general' | 'advanced' = 'general';
  export let isAdmin = false;

  let activeTab: 'general' | 'advanced' = initialTab;

  // A non-admin can never sit on a tab that does not exist for them.
  $: if (!isAdmin && activeTab === 'advanced') activeTab = 'general';

  $: tabs = [
    { id: 'general' as const, label: $t('chat.settings.tabs.general') },
    ...(isAdmin ? [{ id: 'advanced' as const, label: $t('chat.settings.tabs.advanced') }] : []),
  ];
</script>

<div class="chat-settings-panel" data-testid="chat-settings-panel">
  {#if tabs.length > 1}
    <div class="tabs" role="tablist">
      {#each tabs as tab (tab.id)}
        <button
          type="button"
          class="tab"
          class:active={activeTab === tab.id}
          role="tab"
          aria-selected={activeTab === tab.id}
          on:click={() => (activeTab = tab.id)}
          data-testid="chat-settings-tab-{tab.id}"
        >
          {tab.label}
        </button>
      {/each}
    </div>
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

  .tabs {
    display: flex;
    gap: 0.5rem;
    border-bottom: 1px solid var(--border-color);
    margin-bottom: 1.25rem;
  }

  .tab {
    padding: 0.5rem 1rem;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    cursor: pointer;
    color: var(--text-secondary);
    font-size: 0.875rem;
    font-weight: 500;
    /* form-elements.css styles every bare <button>; without these the tab grows
       a surface fill, a shadow and a hover scale. */
    box-shadow: none;
  }

  .tab:hover {
    color: var(--text-color);
    background: none;
    transform: none;
    box-shadow: none;
  }

  .tab.active {
    color: var(--primary-color);
    border-bottom-color: var(--primary-color);
  }

  .tab:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
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

  @media (max-width: 640px) {
    .tab {
      min-height: 44px;
      flex: 1;
      text-align: center;
    }
  }
</style>
