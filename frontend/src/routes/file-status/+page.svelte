<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import UserFileStatus from '$components/UserFileStatus.svelte';
  import { settingsReturnFromUrl } from '$lib/settings/returnToSettings';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';

  // Only present when the user arrived through a Settings panel's "Open full screen" link.
  $: backTarget = settingsReturnFromUrl($page.url);

  async function backToSettings() {
    if (!backTarget) return;
    const { section, path } = backTarget;
    await goto(path);
    settingsModalStore.open(section);
  }
</script>

<div class="file-status-page">
  <UserFileStatus>
    <!-- Inside the dashboard's own centred column, so the control lines up with its title. -->
    <svelte:fragment slot="top">
      {#if backTarget}
        <button
          type="button"
          class="back-to-settings"
          data-testid="back-to-settings"
          on:click={backToSettings}
        >
          <svg
            class="back-icon"
            xmlns="http://www.w3.org/2000/svg"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <polyline points="15 18 9 12 15 6"></polyline>
          </svg>
          {$t('fileStatus.backToSettings')}
        </button>
      {/if}
    </svelte:fragment>
  </UserFileStatus>
</div>

<style>
  .file-status-page {
    width: 100%;
  }

  .back-to-settings {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    margin-bottom: 0.5rem;
    padding: 0.25rem 0.5rem;
    border: none;
    border-radius: 6px;
    background: none;
    box-shadow: none;
    color: var(--primary-on-surface);
    font: inherit;
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
  }

  .back-to-settings:hover {
    background: rgba(var(--primary-color-rgb), 0.08);
    box-shadow: none;
    transform: none;
  }

  .back-to-settings:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  /* The chevron points toward the start edge, which is the right edge in RTL. */
  :global([dir='rtl']) .back-icon {
    transform: scaleX(-1);
  }
</style>
