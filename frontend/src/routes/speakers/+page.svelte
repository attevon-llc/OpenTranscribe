<script lang="ts">
  import { resolve } from '$app/paths';
  import { onMount, onDestroy } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import { page } from '$app/state';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import ClustersSection from '$components/speakers/ClustersSection.svelte';
  import ProfilesSection from '$components/speakers/ProfilesSection.svelte';
  import InboxTab from '$components/speakers/InboxTab.svelte';
  import SpeakerPreviewPlayer from '$components/speakers/SpeakerPreviewPlayer.svelte';
  import RetrievalQualityNotice from '$components/RetrievalQualityNotice.svelte';
  import { audioPlaybackStore } from '$stores/audioPlaybackStore';
  import {
    getSpeakerMediaPreview,
    RENAME_PROPAGATION_TIMEOUT_MS
  } from '$lib/api/speakerClusters';
  import type { SpeakerMediaPreviewData } from '$lib/api/speakerClusters';
  import type { SpeakerInboxItem as InboxItem, SpeakerProfile } from '$lib/types/speakerCluster';
  import SpeakerPrivacyNotice from '$components/privacy/SpeakerPrivacyNotice.svelte';
  import { createPreviewCache } from '$lib/speakers/previewCache';
  import { createRenamePropagationTracker } from '$lib/speakers/renamePropagation';
  import { fetchProfiles, fetchInboxPage } from '$lib/speakers/loaders';
  import { performInboxAction } from '$lib/speakers/inboxActions';
  import { resolveInboxShortcut } from '$lib/speakers/pageHelpers';

  type Tab = 'clusters' | 'profiles' | 'inbox';
  // Deep link from elsewhere in the app — currently the file-detail speaker
  // list's "has a profile" badge (`transcript/SpeakerEditorPanel.svelte`),
  // which links here as `/speakers?tab=profiles` so a labeled speaker's
  // profile is one click from where it was noticed rather than a manual
  // tab switch + hunt.
  const initialTab = page.url.searchParams.get('tab');
  let activeTab: Tab = initialTab === 'profiles' || initialTab === 'inbox' ? initialTab : 'clusters';

  // The Clusters tab owns its list and flows; the page needs its total (tab badge) and a handle.
  let clustersSection: ClustersSection;
  let clusterTotal = 0;

  // Profiles state (edit / delete / avatar / gender flows live in ProfilesSection)
  let profiles: SpeakerProfile[] = [];
  let loadingProfiles = false;

  // Inbox state
  let inboxItems: InboxItem[] = [];
  let inboxTotal = 0;
  let inboxPage = 1;
  let inboxPages = 0;
  let loadingInbox = false;
  let inboxActionInProgress: Set<string> = new SvelteSet();

  // Sticky floating player state
  let speakerPreviewData: SpeakerMediaPreviewData | null = null;
  let previewCurrentTime = 0;
  let previewPlayerRef: ReturnType<typeof import('$components/FloatingPreviewPlayer.svelte').default> | null = null;
  const previewCache = createPreviewCache(getSpeakerMediaPreview);

  // Rename-propagation progress. Every rename here — a cluster label edit, promoting a
  // cluster to a profile, or accepting an inbox suggestion — queues per-file digest
  // regeneration server-side, reported back as one `speaker-rename-propagation` WebSocket
  // message per batch (backend/app/tasks/rename_propagation_task.py).
  const renameTracker = createRenamePropagationTracker(RENAME_PROPAGATION_TIMEOUT_MS);
  const pendingRenamePropagations = renameTracker.pending;

  function handleRenamePropagation(event: Event) {
    const detail = (event as CustomEvent).detail as
      | { new_name?: string; regenerated?: number; errors?: number; total?: number }
      | undefined;
    renameTracker.settle();

    const regenerated = detail?.regenerated ?? 0;
    if (regenerated > 0) {
      toastStore.success(
        $t('speakers.renamePropagationComplete', { count: regenerated, name: detail?.new_name ?? '' })
      );
    }
    // Refresh whichever tab is showing summaries/profiles derived from the
    // renamed speaker, so the page doesn't keep displaying stale data next to
    // a toast that just said it changed.
    if (activeTab === 'profiles') loadProfiles(true);
  }

  onMount(async () => {
    const results = await Promise.allSettled([
      clustersSection.load(true),
      loadProfiles(true),
      loadInbox(true)
    ]);
    const failures = results.filter(r => r.status === 'rejected');
    if (failures.length > 0) {
      toastStore.error($t('speakers.error.loadSections', { count: failures.length }));
    }
    window.addEventListener('speaker-rename-propagation', handleRenamePropagation);
  });

  onDestroy(() => {
    window.removeEventListener('speaker-rename-propagation', handleRenamePropagation);
    renameTracker.dispose();
  });

  function switchTab(tab: Tab) {
    activeTab = tab;
    clustersSection.resetModes();
    speakerPreviewData = null;
    if (tab === 'clusters') clustersSection.load();
    if (tab === 'profiles') loadProfiles();
    if (tab === 'inbox') loadInbox();
  }

  // --- Data loading ---

  async function loadProfiles(silent = false) {
    loadingProfiles = true;
    try {
      profiles = await fetchProfiles();
    } catch (err) {
      if (!silent) toastStore.error($t('speakers.error.loadProfiles'));
      throw err;
    } finally {
      loadingProfiles = false;
    }
  }

  async function loadInbox(silent = false) {
    loadingInbox = true;
    try {
      const res = await fetchInboxPage(inboxPage);
      inboxItems = res.items;
      inboxTotal = res.total;
      inboxPages = res.pages;
    } catch (err) {
      if (!silent) toastStore.error($t('speakers.error.loadInbox'));
      throw err;
    } finally {
      loadingInbox = false;
    }
  }

  // --- Sticky floating player ---

  async function openSpeakerPreview(speakerUuid: string) {
    // If same speaker is already playing, pause instead
    if ($audioPlaybackStore.activeSpeakerUuid === speakerUuid && $audioPlaybackStore.isPlaying) {
      previewPlayerRef?.getPlayer()?.pause();
      return;
    }
    try {
      speakerPreviewData = await previewCache.load(speakerUuid);
      previewCurrentTime = speakerPreviewData?.start_time ?? 0;
      audioPlaybackStore.play(speakerUuid);
    } catch {
      toastStore.error($t('speakers.inbox.previewUnavailable'));
      speakerPreviewData = null;
      audioPlaybackStore.stop();
    }
  }

  function closeSpeakerPreview() {
    speakerPreviewData = null;
    audioPlaybackStore.stop();
  }

  function handlePreviewPlay() {
    if (speakerPreviewData) {
      audioPlaybackStore.play(speakerPreviewData.speaker_uuid);
    }
  }

  function handlePreviewPause() {
    audioPlaybackStore.pause();
  }

  function handlePreviewTimeUpdate(e: CustomEvent<{ currentTime: number }>) {
    previewCurrentTime = e.detail.currentTime;
  }

  // --- Inbox ---

  async function handleInboxAction(
    e: CustomEvent<{ type: string; speaker_uuid: string; profile_uuid?: string }>
  ) {
    const { type, speaker_uuid, profile_uuid } = e.detail;
    if (inboxActionInProgress.has(speaker_uuid)) return;
    inboxActionInProgress.add(speaker_uuid);
    inboxActionInProgress = inboxActionInProgress; // trigger reactivity
    try {
      const item = inboxItems.find(i => i.speaker_uuid === speaker_uuid);
      const outcome = await performInboxAction(type, speaker_uuid, profile_uuid, item?.suggested_name);
      if (outcome !== 'ignored') {
        inboxItems = inboxItems.filter((i) => i.speaker_uuid !== speaker_uuid);
        inboxTotal = Math.max(0, inboxTotal - 1);
      }
      if (outcome === 'accepted') {
        toastStore.success($t('speakers.inbox.accepted'));
        renameTracker.begin();
      } else if (outcome === 'assigned') {
        toastStore.success($t('speakers.inbox.assigned'));
        renameTracker.begin();
      }
    } catch {
      toastStore.error($t('speakers.error.verify'));
    } finally {
      inboxActionInProgress.delete(speaker_uuid);
      inboxActionInProgress = inboxActionInProgress;
    }
  }

  // --- Keyboard shortcuts ---

  function handleKeydown(e: KeyboardEvent) {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;

    // Escape cancels merge/split mode from any tab
    if (e.key === 'Escape') {
      if (clustersSection.cancelActiveMode()) return;
      if (speakerPreviewData) { closeSpeakerPreview(); return; }
    }

    if (activeTab !== 'inbox' || !inboxItems.length) return;

    const first = inboxItems[0];
    const shortcut = resolveInboxShortcut(e.key, first);
    if (shortcut === 'accept' || shortcut === 'skip') {
      handleInboxAction(
        new CustomEvent('action', { detail: { type: shortcut, speaker_uuid: first.speaker_uuid } })
      );
    } else if (shortcut === 'preview') {
      openSpeakerPreview(first.speaker_uuid);
    }
  }
</script>

<svelte:head>
  <title>{$t('speakers.title')} - OpenTranscribe</title>
</svelte:head>

<svelte:window on:keydown={handleKeydown} />

<div class="speakers-page">
  <div class="page-header">
    <a href={resolve('/')} class="back-to-gallery" title={$t('nav.backToGallery')}>
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="15 18 9 12 15 6"></polyline>
      </svg>
    </a>
    <h1>{$t('speakers.title')}</h1>
    {#if $pendingRenamePropagations > 0}
      <span
        class="rename-propagation-indicator"
        data-testid="rename-propagation-indicator"
        title={$t('speakers.renamePropagationPending')}
      >
        <span class="rename-propagation-spinner" aria-hidden="true"></span>
        {$t('speakers.renamePropagationPending')}
      </span>
    {/if}
  </div>

  <!-- Issue #756: clustering quality + gender/attribute metrics are still evolving here;
       suggestions are never auto-applied. A distinct surface from chat/search (#461) — see
       RetrievalQualityNotice's header comment. -->
  <RetrievalQualityNotice surface="speakers" />

  <div class="tabs">
    <button class="tab" class:active={activeTab === 'clusters'} on:click={() => switchTab('clusters')}>
      {$t('speakers.tabs.clusters')}
      {#if clusterTotal > 0}
        <span class="badge">{clusterTotal}</span>
      {/if}
    </button>
    <button class="tab" class:active={activeTab === 'profiles'} on:click={() => switchTab('profiles')}>
      {$t('speakers.tabs.profiles')}
      {#if profiles.length > 0}
        <span class="badge">{profiles.length}</span>
      {/if}
    </button>
    <button
      class="tab"
      class:active={activeTab === 'inbox'}
      on:click={() => switchTab('inbox')}
      data-testid="speakers-tab-inbox"
    >
      {$t('speakers.tabs.inbox')}
      {#if inboxTotal > 0}
        <span class="badge alert">{inboxTotal}</span>
      {/if}
    </button>
  </div>

  <SpeakerPrivacyNotice show={activeTab === 'profiles' ? 'gender' : 'both'} />

  <ClustersSection
    bind:this={clustersSection}
    active={activeTab === 'clusters'}
    bind:clusterTotal
    reviewCount={inboxTotal}
    onRenameStarted={renameTracker.begin}
    onPreview={openSpeakerPreview}
    onPrefetch={previewCache.prefetch}
    onOpenReview={() => switchTab('inbox')}
  />

  <ProfilesSection
    active={activeTab === 'profiles'}
    bind:profiles
    {loadingProfiles}
    reload={loadProfiles}
  />

  <!-- Inbox Tab -->
  {#if activeTab === 'inbox'}
    <InboxTab
      {loadingInbox}
      {inboxItems}
      {profiles}
      {inboxActionInProgress}
      {inboxPage}
      {inboxPages}
      on:action={handleInboxAction}
      on:preview={(e) => openSpeakerPreview(e.detail.speaker_uuid)}
      on:prefetch={(e) => previewCache.prefetch(e.detail.speaker_uuid)}
      on:prevPage={() => { inboxPage--; loadInbox(); }}
      on:nextPage={() => { inboxPage++; loadInbox(); }}
    />
  {/if}
</div>

<!-- Sticky Floating Preview Player -->
<SpeakerPreviewPlayer
  {speakerPreviewData}
  {previewCurrentTime}
  bind:playerRef={previewPlayerRef}
  on:close={closeSpeakerPreview}
  on:timeupdate={handlePreviewTimeUpdate}
  on:play={handlePreviewPlay}
  on:pause={handlePreviewPause}
/>

<style>
  .speakers-page {
    max-width: 1000px;
    margin: 0 auto;
    padding: 24px 16px;
    overflow-x: hidden;
    max-width: 100vw;
    box-sizing: border-box;
  }

  @media (min-width: 769px) {
    .speakers-page {
      max-width: 1000px;
    }
  }

  .page-header {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    margin-bottom: 16px;
  }

  .page-header h1 {
    font-size: 24px;
    font-weight: 600;
    color: var(--text-color);
    margin: 0;
  }

  .rename-propagation-indicator {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: 0.5rem;
    padding: 0.25rem 0.65rem;
    border-radius: 999px;
    background-color: rgba(var(--primary-color-rgb), 0.1);
    color: var(--primary-on-surface);
    font-size: 0.78rem;
    font-weight: 500;
  }

  .rename-propagation-spinner {
    width: 0.8rem;
    height: 0.8rem;
    border: 2px solid rgba(var(--primary-color-rgb), 0.3);
    border-top-color: var(--primary-color);
    border-radius: 50%;
    animation: rename-propagation-spin 0.7s linear infinite;
  }

  @keyframes rename-propagation-spin {
    to {
      transform: rotate(360deg);
    }
  }

  .back-to-gallery {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: 6px;
    color: var(--text-secondary);
    text-decoration: none;
    flex-shrink: 0;
    transition: background 0.15s, color 0.15s;
  }

  .back-to-gallery:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .tabs {
    display: flex;
    border-bottom: 2px solid var(--border-color);
    margin-bottom: 20px;
  }

  .tab {
    padding: 10px 20px;
    border: none;
    border-radius: 0;
    background: none;
    color: var(--text-secondary);
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    transition: color 0.15s ease, border-bottom-color 0.15s ease;
    display: flex;
    align-items: center;
    gap: 8px;
    box-shadow: none;
  }

  .tab:hover,
  .tab:focus {
    color: var(--text-color);
    background: none;
    transform: none;
    box-shadow: none;
  }

  .tab.active {
    color: var(--primary-on-surface);
    border-bottom-color: var(--primary-color);
  }

  .badge {
    font-size: 11px;
    padding: 1px 7px;
    border-radius: 10px;
    background: var(--hover-color);
    color: var(--text-secondary);
  }

  .badge.alert {
    background: var(--error-solid);
    color: var(--text-on-solid);
  }

  @media (max-width: 768px) {
    .speakers-page {
      padding: 16px 12px;
    }

    .page-header h1 {
      font-size: 20px;
      margin-bottom: 12px;
    }

    .tabs {
      gap: 0;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
    }

    .tabs::-webkit-scrollbar {
      display: none;
    }

    .tab {
      padding: 8px 14px;
      font-size: 13px;
      white-space: nowrap;
      flex-shrink: 0;
    }
  }
</style>
