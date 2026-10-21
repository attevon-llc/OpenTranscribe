<!--
  SettingsModalShell.svelte — the Settings dialog as placeholders.

  Painted the moment Settings is asked for, while the real modal (a large chunk) is still
  downloading, so the dialog opens at once and the sidebar and content fill in afterwards.
  The root layout swaps it for `SettingsModal` as soon as that chunk resolves; both render the
  same `SettingsModalFrame`, so nothing jumps.

  Deliberately imports nothing but the frame and the skeleton primitive: this is the part that
  has to be cheap.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';
  import SkeletonLoader from '$components/ui/SkeletonLoader.svelte';
  import SettingsModalFrame from '$components/settings/SettingsModalFrame.svelte';

  // Nothing can be dirty before the real modal exists, so closing needs no confirmation.
  function close() {
    settingsModalStore.close();
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === 'Escape') close();
  }

  onMount(() => {
    lockScroll();
    document.addEventListener('keydown', handleKeyDown);
  });

  onDestroy(() => {
    unlockScroll();
    document.removeEventListener('keydown', handleKeyDown);
  });

  const groups = [4, 3, 5, 3];
</script>

<SettingsModalFrame busy on:close={close}>
  <div slot="sidebar" class="sidebar-skeleton" role="status" aria-label={$t('settings.modal.sectionLoading')}>
    <div class="search-skeleton"></div>
    {#each groups as rows}
      <div class="group-skeleton">
        <div class="heading-skeleton"></div>
        {#each Array(rows) as _}
          <div class="row-skeleton"></div>
        {/each}
      </div>
    {/each}
  </div>

  <div class="content-skeleton" role="status" aria-label={$t('settings.modal.sectionLoading')}>
    <div class="title-skeleton"></div>
    <SkeletonLoader lines={8} height={18} />
  </div>
</SettingsModalFrame>

<style>
  .sidebar-skeleton {
    padding: 0.75rem 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .search-skeleton,
  .heading-skeleton,
  .row-skeleton,
  .title-skeleton {
    border-radius: 6px;
    background: linear-gradient(
      90deg,
      var(--bg-tertiary, #e8e8e8) 25%,
      var(--bg-secondary, #f0f0f0) 50%,
      var(--bg-tertiary, #e8e8e8) 75%
    );
    background-size: 200% 100%;
    animation: shell-pulse 1.5s ease-in-out infinite;
  }

  :global([data-theme='dark']) .search-skeleton,
  :global([data-theme='dark']) .heading-skeleton,
  :global([data-theme='dark']) .row-skeleton,
  :global([data-theme='dark']) .title-skeleton {
    background: linear-gradient(
      90deg,
      rgba(255, 255, 255, 0.06) 25%,
      rgba(255, 255, 255, 0.1) 50%,
      rgba(255, 255, 255, 0.06) 75%
    );
    background-size: 200% 100%;
  }

  .search-skeleton {
    height: 34px;
  }

  .group-skeleton {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .heading-skeleton {
    width: 40%;
    height: 10px;
    margin: 0 0.75rem 0.25rem;
  }

  .row-skeleton {
    height: 26px;
    margin: 0 0.5rem;
  }

  .content-skeleton {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .title-skeleton {
    width: 30%;
    height: 22px;
  }

  @keyframes shell-pulse {
    0% {
      background-position: 200% 0;
    }
    100% {
      background-position: -200% 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .search-skeleton,
    .heading-skeleton,
    .row-skeleton,
    .title-skeleton {
      animation: none;
    }
  }
</style>
