<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { fade } from 'svelte/transition';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import SpeakerClusterCard from '$components/speakers/SpeakerClusterCard.svelte';
  import ClusterMemberList from '$components/speakers/ClusterMemberList.svelte';
  import SpeakerInboxItem from '$components/speakers/SpeakerInboxItem.svelte';
  import { audioPlaybackStore } from '$stores/audioPlaybackStore';
  import { browser } from '$app/environment';
  import Spinner from '../../components/ui/Spinner.svelte';
  import EmptyState from '../../components/ui/EmptyState.svelte';
  import CardGridSkeleton from '$components/ui/CardGridSkeleton.svelte';
  import ListRowSkeleton from '$components/ui/ListRowSkeleton.svelte';

  // Dynamic import: Plyr is browser-only (breaks SSR on page refresh)
  let PlyrMiniPlayer: typeof import('$components/PlyrMiniPlayer.svelte').default | null = null;
  if (browser) {
    import('$components/PlyrMiniPlayer.svelte').then(m => { PlyrMiniPlayer = m.default; });
  }
  import {
    listClusters,
    getClusterDetail,
    updateCluster,
    deleteCluster,
    promoteCluster,
    mergeClusters,
    splitCluster,
    triggerRecluster,
    getUnverifiedSpeakers,
    batchVerifySpeakers,
    getSpeakerMediaPreview,
    updateProfile,
    deleteProfile,
    listProfiles,
    uploadProfileAvatar,
    deleteProfileAvatar,
    confirmProfileGender,
    unassignSpeakers
  } from '$lib/api/speakerClusters';
  import type { SpeakerMediaPreviewData } from '$lib/api/speakerClusters';
  import type {
    SpeakerCluster,
    SpeakerClusterMember,
    SpeakerInboxItem as InboxItem,
    SpeakerProfile
  } from '$lib/types/speakerCluster';
  import { apiCache } from '$lib/apiCache';

  type Tab = 'clusters' | 'profiles' | 'inbox';
  let activeTab: Tab = 'clusters';

  // Clusters state
  let clusters: SpeakerCluster[] = [];
  let clusterTotal = 0;
  let clusterPage = 1;
  let clusterPages = 0;
  let clusterSearch = '';
  let expandedCluster: string | null = null;
  let clusterMembers: Record<string, SpeakerClusterMember[]> = {};
  let loadingClusters = false;
  let reclustering = false;
  let reclusterTimeout: ReturnType<typeof setTimeout> | null = null;
  let labeledCount = 0;
  let unlabeledCount = 0;
  let lastClusteredAt: string | null = null;

  // Collapsible sections state
  let identifiedCollapsed = false;
  let unidentifiedCollapsed = false;

  // Avatar upload state
  let avatarUploading: Set<string> = new Set();

  // Derived arrays for collapsible sections
  $: identifiedClusters = clusters.filter(c => c.promoted_to_profile_name || c.label);
  $: unidentifiedClusters = clusters.filter(c => !(c.promoted_to_profile_name || c.label));

  // Merge state
  let mergeMode = false;
  let mergeSourceUuid: string | null = null;

  // Split state
  let splitMode = false;
  let splitTargetUuid: string | null = null;
  let splitSelectedMembers: Set<string> = new Set();

  // Unassign state
  let unassignMode = false;
  let unassignTargetUuid: string | null = null;
  let unassignSelectedMembers: Set<string> = new Set();
  let unassignBlacklist = true;

  // Outlier data per cluster (populated by ClusterMemberList embedding analysis)
  let clusterOutlierData: Record<string, { outlierUuids: string[] }> = {};

  // Profiles state
  let profiles: SpeakerProfile[] = [];
  let loadingProfiles = false;
  let genderConfirmedProfiles: Set<string> = new Set();

  // Inbox state
  let inboxItems: InboxItem[] = [];
  let inboxTotal = 0;
  let inboxPage = 1;
  let inboxPages = 0;
  let loadingInbox = false;
  let inboxActionInProgress: Set<string> = new Set();

  // Search debounce
  let searchTimeout: ReturnType<typeof setTimeout>;

  // Delete modal state
  let showDeleteModal = false;
  let deleteTargetUuid = '';

  // Profile edit/delete state
  let editingProfileUuid: string | null = null;
  let editProfileName = '';
  let deleteProfileUuid = '';
  let showDeleteProfileModal = false;

  // Sticky floating player state
  let speakerPreviewData: SpeakerMediaPreviewData | null = null;
  let previewCurrentTime = 0;
  let previewPlayerRef: any = null;

  // Promote modal state
  let showPromoteModal = false;
  let promoteTargetUuid = '';
  let promoteNameInput = '';

  // Clustering progress state (via WebSocket)
  let clusteringProgress: { step: number; total_steps: number; message: string; progress: number } | null = null;

  // --- WebSocket event handlers ---

  function handleClusteringProgress(event: Event) {
    const detail = (event as CustomEvent).detail;
    if (detail?.progress != null && detail?.total_steps != null) {
      clusteringProgress = detail;
      reclustering = detail.running !== false;
    }
  }

  function handleClusteringComplete(_event: Event) {
    if (reclusterTimeout) clearTimeout(reclusterTimeout);
    reclustering = false;
    clusteringProgress = null;
    loadClusters();
    toastStore.success($t('speakers.reclusterComplete'));
  }

  function handleClusteringFileComplete() {
    if (activeTab === 'clusters') loadClusters();
  }

  onMount(async () => {
    // Restore collapse state from localStorage
    try {
      const saved = localStorage.getItem('speakers-cluster-sections-collapse');
      if (saved) {
        const parsed = JSON.parse(saved);
        identifiedCollapsed = parsed.identified ?? false;
        unidentifiedCollapsed = parsed.unidentified ?? false;
      }
    } catch { /* ignore */ }

    const results = await Promise.allSettled([
      loadClusters(true),
      loadProfiles(true),
      loadInbox(true)
    ]);
    const failures = results.filter(r => r.status === 'rejected');
    if (failures.length > 0) {
      toastStore.error($t('speakers.error.loadSections', { count: failures.length }));
    }
    window.addEventListener('clustering-progress', handleClusteringProgress);
    window.addEventListener('clustering-complete', handleClusteringComplete);
    window.addEventListener('clustering-file-complete', handleClusteringFileComplete);
  });

  onDestroy(() => {
    window.removeEventListener('clustering-progress', handleClusteringProgress);
    window.removeEventListener('clustering-complete', handleClusteringComplete);
    window.removeEventListener('clustering-file-complete', handleClusteringFileComplete);
    clearTimeout(searchTimeout);
    if (reclusterTimeout) clearTimeout(reclusterTimeout);
  });

  // --- Section collapse ---

  function toggleSection(section: 'identified' | 'unidentified') {
    if (section === 'identified') identifiedCollapsed = !identifiedCollapsed;
    else unidentifiedCollapsed = !unidentifiedCollapsed;
    try {
      localStorage.setItem('speakers-cluster-sections-collapse', JSON.stringify({
        identified: identifiedCollapsed,
        unidentified: unidentifiedCollapsed
      }));
    } catch { /* ignore */ }
  }

  function getInitials(name: string): string {
    return name.split(/\s+/).map(w => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();
  }

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

  async function handleAvatarUpload(profileUuid: string, event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    input.value = '';

    // Validate type
    const allowed = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];
    if (!allowed.includes(file.type)) {
      toastStore.error($t('speakers.avatar.invalidType'));
      return;
    }
    // Validate size (2MB)
    if (file.size > 2 * 1024 * 1024) {
      toastStore.error($t('speakers.avatar.tooLarge'));
      return;
    }

    if (avatarUploading.has(profileUuid)) return;
    avatarUploading.add(profileUuid);
    avatarUploading = avatarUploading;

    try {
      const result = await uploadProfileAvatar(profileUuid, file);
      // Update the profile in the local array
      profiles = profiles.map(p => p.uuid === profileUuid ? { ...p, avatar_url: result.avatar_url } : p);
      toastStore.success($t('speakers.avatar.uploaded'));
    } catch {
      toastStore.error($t('speakers.avatar.error'));
    } finally {
      avatarUploading.delete(profileUuid);
      avatarUploading = avatarUploading;
    }
  }

  // --- Tab switching ---

  function switchTab(tab: Tab) {
    activeTab = tab;
    mergeMode = false;
    mergeSourceUuid = null;
    splitMode = false;
    splitTargetUuid = null;
    splitSelectedMembers = new Set();
    unassignMode = false;
    unassignTargetUuid = null;
    unassignSelectedMembers = new Set();
    speakerPreviewData = null;
    if (tab === 'clusters') loadClusters();
    if (tab === 'profiles') loadProfiles();
    if (tab === 'inbox') loadInbox();
  }

  // --- Data loading ---

  async function loadClusters(silent = false) {
    loadingClusters = true;
    try {
      // Use prefetched data for initial default load (page 1, no search)
      const prefetchKey = 'speakers:clusters';
      const cached = (clusterPage === 1 && !clusterSearch) ? apiCache.get<any>(prefetchKey) : null;
      const res = cached ?? await listClusters(clusterPage, 20, clusterSearch || undefined);
      if (cached) apiCache.invalidate(prefetchKey);
      clusters = res.items;
      clusterTotal = res.total;
      clusterPages = res.pages;
      labeledCount = res.labeled_count ?? 0;
      unlabeledCount = res.unlabeled_count ?? 0;
      lastClusteredAt = res.last_clustered_at ?? null;
    } catch (err) {
      if (!silent) toastStore.error($t('speakers.error.loadClusters'));
      throw err;
    } finally {
      loadingClusters = false;
    }
  }

  async function loadProfiles(silent = false) {
    loadingProfiles = true;
    try {
      // Use prefetched data if available
      const prefetchKey = 'speakers:profiles';
      const cached = apiCache.get<SpeakerProfile[]>(prefetchKey);
      profiles = cached ?? await listProfiles();
      if (cached) apiCache.invalidate(prefetchKey);
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
      // Use prefetched data for initial default load (page 1)
      const prefetchKey = 'speakers:inbox';
      const cached = (inboxPage === 1) ? apiCache.get<any>(prefetchKey) : null;
      const res = cached ?? await getUnverifiedSpeakers(inboxPage, 20);
      if (cached) apiCache.invalidate(prefetchKey);
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

  // --- Cluster actions ---

  async function handleClusterExpand(e: CustomEvent<{ uuid: string }>) {
    const uuid = e.detail.uuid;
    expandedCluster = uuid;
    if (!clusterMembers[uuid]) {
      try {
        const detail = await getClusterDetail(uuid);
        clusterMembers[uuid] = detail.members;
        clusterMembers = clusterMembers;
      } catch {
        // silently fail
      }
    }
  }

  async function handleClusterUpdate(e: CustomEvent<{ uuid: string; label: string }>) {
    try {
      await updateCluster(e.detail.uuid, { label: e.detail.label });
      await loadClusters();
    } catch {
      toastStore.error($t('speakers.error.updateCluster'));
    }
  }

  // Delete cluster (modal)
  async function handleClusterDelete(e: CustomEvent<{ uuid: string }>) {
    deleteTargetUuid = e.detail.uuid;
    showDeleteModal = true;
  }

  async function confirmDelete() {
    try {
      await deleteCluster(deleteTargetUuid);
      toastStore.success($t('speakers.cluster.deleted'));
      await loadClusters();
      if (clusterPage > clusterPages && clusterPages > 0) {
        clusterPage = clusterPages;
        await loadClusters();
      }
    } catch {
      toastStore.error($t('speakers.error.delete'));
    }
    showDeleteModal = false;
    deleteTargetUuid = '';
  }

  // Promote cluster (modal with text input)
  async function handleClusterPromote(e: CustomEvent<{ uuid: string }>) {
    promoteTargetUuid = e.detail.uuid;
    const targetCluster = clusters.find(c => c.uuid === promoteTargetUuid);
    promoteNameInput = targetCluster?.label || targetCluster?.suggested_name || '';
    showPromoteModal = true;
  }

  async function confirmPromote() {
    if (!promoteNameInput.trim()) return;
    try {
      await promoteCluster(promoteTargetUuid, promoteNameInput.trim());
      toastStore.success($t('speakers.cluster.promoted'));
      await loadClusters();
    } catch {
      toastStore.error($t('speakers.error.promote'));
    }
    showPromoteModal = false;
  }

  // Merge flow
  async function handleClusterMerge(e: CustomEvent<{ uuid: string }>) {
    if (mergeMode && mergeSourceUuid) {
      // Second click: this is the target
      const targetUuid = e.detail.uuid;
      if (targetUuid === mergeSourceUuid) {
        toastStore.error($t('speakers.merge.cannotSelf'));
        return;
      }
      try {
        await mergeClusters(mergeSourceUuid, targetUuid);
        toastStore.success($t('speakers.merge.success'));
        mergeMode = false;
        mergeSourceUuid = null;
        await loadClusters();
        if (clusterPage > clusterPages && clusterPages > 0) {
          clusterPage = clusterPages;
          await loadClusters();
        }
      } catch {
        mergeMode = false;
        mergeSourceUuid = null;
        toastStore.error($t('speakers.merge.error'));
      }
    } else {
      // First click: enter merge mode
      mergeSourceUuid = e.detail.uuid;
      mergeMode = true;
      // Auto-expand both sections so user can pick any target
      identifiedCollapsed = false;
      unidentifiedCollapsed = false;
      toastStore.info($t('speakers.merge.selectTarget'));
    }
  }

  function cancelMerge() {
    mergeMode = false;
    mergeSourceUuid = null;
  }

  // Split flow
  async function handleClusterSplit(e: CustomEvent<{ uuid: string }>) {
    const uuid = e.detail.uuid;
    // Auto-expand both sections during split mode
    identifiedCollapsed = false;
    unidentifiedCollapsed = false;
    // Auto-expand to show members
    if (expandedCluster !== uuid) {
      expandedCluster = uuid;
      if (!clusterMembers[uuid]) {
        try {
          const detail = await getClusterDetail(uuid);
          clusterMembers[uuid] = detail.members;
          clusterMembers = clusterMembers;
        } catch {
          toastStore.error($t('speakers.error.loadClusters'));
          return;
        }
      }
    }
    splitTargetUuid = uuid;
    splitMode = true;
    splitSelectedMembers = new Set();
  }

  async function confirmSplit() {
    if (!splitTargetUuid || splitSelectedMembers.size === 0) return;
    try {
      await splitCluster(splitTargetUuid, Array.from(splitSelectedMembers));
      toastStore.success($t('speakers.split.success'));
      splitMode = false;
      splitTargetUuid = null;
      splitSelectedMembers = new Set();
      await loadClusters();
    } catch {
      splitMode = false;
      splitTargetUuid = null;
      splitSelectedMembers = new Set();
      toastStore.error($t('speakers.split.error'));
    }
  }

  function cancelSplit() {
    splitMode = false;
    splitTargetUuid = null;
    splitSelectedMembers = new Set();
  }

  function toggleSplitMember(speakerUuid: string) {
    if (splitSelectedMembers.has(speakerUuid)) {
      splitSelectedMembers.delete(speakerUuid);
    } else {
      splitSelectedMembers.add(speakerUuid);
    }
    splitSelectedMembers = splitSelectedMembers;
  }

  // Unassign flow
  async function handleClusterUnassign(e: CustomEvent<{ uuid: string }>) {
    const uuid = e.detail.uuid;
    identifiedCollapsed = false;
    unidentifiedCollapsed = false;
    if (expandedCluster !== uuid) {
      expandedCluster = uuid;
      if (!clusterMembers[uuid]) {
        try {
          const detail = await getClusterDetail(uuid);
          clusterMembers[uuid] = detail.members;
          clusterMembers = clusterMembers;
        } catch {
          toastStore.error($t('speakers.error.loadClusters'));
          return;
        }
      }
    }
    unassignTargetUuid = uuid;
    unassignMode = true;
    // Pre-select outliers if analysis data exists for this cluster
    const outlierData = clusterOutlierData[uuid];
    unassignSelectedMembers = outlierData?.outlierUuids?.length ? new Set(outlierData.outlierUuids) : new Set();
    unassignBlacklist = true;
  }

  async function confirmUnassign() {
    if (!unassignTargetUuid || unassignSelectedMembers.size === 0) return;
    try {
      await unassignSpeakers(unassignTargetUuid, Array.from(unassignSelectedMembers), unassignBlacklist);
      toastStore.success($t('speakers.unassign.success'));
      unassignMode = false;
      unassignTargetUuid = null;
      unassignSelectedMembers = new Set();
      await loadClusters();
    } catch {
      unassignMode = false;
      unassignTargetUuid = null;
      unassignSelectedMembers = new Set();
      toastStore.error($t('speakers.split.error'));
    }
  }

  function cancelUnassign() {
    unassignMode = false;
    unassignTargetUuid = null;
    unassignSelectedMembers = new Set();
  }

  function toggleUnassignMember(speakerUuid: string) {
    if (unassignSelectedMembers.has(speakerUuid)) {
      unassignSelectedMembers.delete(speakerUuid);
    } else {
      unassignSelectedMembers.add(speakerUuid);
    }
    unassignSelectedMembers = unassignSelectedMembers;
  }

  function handleOutlierAnalysisComplete(e: CustomEvent<{ clusterUuid: string; outlierUuids: string[] }>) {
    const { clusterUuid, outlierUuids } = e.detail;
    clusterOutlierData[clusterUuid] = { outlierUuids };
    clusterOutlierData = clusterOutlierData;
  }

  // Recluster
  async function handleRecluster() {
    reclustering = true;
    if (reclusterTimeout) clearTimeout(reclusterTimeout);
    reclusterTimeout = setTimeout(() => {
      if (reclustering) {
        reclustering = false;
        clusteringProgress = null;
        toastStore.error($t('speakers.error.reclusterTimeout'));
      }
    }, 5 * 60 * 1000);
    try {
      await triggerRecluster();
    } catch {
      reclustering = false;
      if (reclusterTimeout) clearTimeout(reclusterTimeout);
      toastStore.error($t('speakers.error.recluster'));
    }
  }

  // --- Profile management ---

  function startEditProfile(profile: SpeakerProfile) {
    editingProfileUuid = profile.uuid;
    editProfileName = profile.name || '';
  }

  function cancelEditProfile() {
    editingProfileUuid = null;
    editProfileName = '';
  }

  async function saveProfile(uuid: string) {
    if (!editProfileName.trim()) { cancelEditProfile(); return; }
    try {
      await updateProfile(uuid, { name: editProfileName.trim() });
      await loadProfiles();
    } catch {
      toastStore.error($t('speakers.error.updateProfile'));
    }
    cancelEditProfile();
  }

  function confirmDeleteProfile(uuid: string) {
    deleteProfileUuid = uuid;
    showDeleteProfileModal = true;
  }

  async function handleDeleteProfile() {
    try {
      await deleteProfile(deleteProfileUuid);
      toastStore.success($t('speakers.profiles.deleted'));
      await loadProfiles();
    } catch {
      toastStore.error($t('speakers.error.deleteProfile'));
    }
    showDeleteProfileModal = false;
    deleteProfileUuid = '';
  }

  async function handleConfirmProfileGender(profile: SpeakerProfile, gender: string) {
    const oldGender = profile.predicted_gender;
    // Optimistic update
    profiles = profiles.map(p =>
      p.uuid === profile.uuid ? { ...p, predicted_gender: gender } : p
    );
    genderConfirmedProfiles.add(profile.uuid);
    genderConfirmedProfiles = genderConfirmedProfiles;
    try {
      await confirmProfileGender(profile.uuid, gender);
    } catch {
      // Revert on error
      profiles = profiles.map(p =>
        p.uuid === profile.uuid ? { ...p, predicted_gender: oldGender } : p
      );
      genderConfirmedProfiles.delete(profile.uuid);
      genderConfirmedProfiles = genderConfirmedProfiles;
      toastStore.error($t('speakers.error.confirmGender'));
    }
  }

  // --- Sticky floating player ---

  // Prefetch cache for speaker media previews (hover-triggered)
  const previewCache = new Map<string, { data: SpeakerMediaPreviewData; ts: number }>();
  const prefetchInFlight = new Set<string>();
  const CACHE_TTL = 5 * 60 * 1000; // 5 minutes

  function prefetchSpeakerPreview(speakerUuid: string) {
    const cached = previewCache.get(speakerUuid);
    if (cached && Date.now() - cached.ts < CACHE_TTL) return;
    if (prefetchInFlight.has(speakerUuid)) return;
    prefetchInFlight.add(speakerUuid);
    getSpeakerMediaPreview(speakerUuid)
      .then(data => {
        previewCache.set(speakerUuid, { data, ts: Date.now() });
      })
      .catch(() => { /* silent prefetch failure */ })
      .finally(() => { prefetchInFlight.delete(speakerUuid); });
  }

  async function openSpeakerPreview(speakerUuid: string) {
    // If same speaker is already playing, pause instead
    if ($audioPlaybackStore.activeSpeakerUuid === speakerUuid && $audioPlaybackStore.isPlaying) {
      previewPlayerRef?.getPlayer()?.pause();
      return;
    }
    try {
      const cached = previewCache.get(speakerUuid);
      if (cached && Date.now() - cached.ts < CACHE_TTL) {
        speakerPreviewData = cached.data;
      } else {
        speakerPreviewData = await getSpeakerMediaPreview(speakerUuid);
        previewCache.set(speakerUuid, { data: speakerPreviewData, ts: Date.now() });
      }
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

  function formatPlaybackTime(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  // --- Inbox ---

  async function handleInboxAction(e: CustomEvent<{ type: string; speaker_uuid: string }>) {
    const { type, speaker_uuid } = e.detail;
    if (inboxActionInProgress.has(speaker_uuid)) return;
    inboxActionInProgress.add(speaker_uuid);
    inboxActionInProgress = inboxActionInProgress; // trigger reactivity
    try {
      if (type === 'accept') {
        const item = inboxItems.find(i => i.speaker_uuid === speaker_uuid);
        await batchVerifySpeakers([speaker_uuid], 'accept', undefined, item?.suggested_name || undefined);
        inboxItems = inboxItems.filter((i) => i.speaker_uuid !== speaker_uuid);
        inboxTotal = Math.max(0, inboxTotal - 1);
        toastStore.success($t('speakers.inbox.accepted'));
      } else if (type === 'skip') {
        await batchVerifySpeakers([speaker_uuid], 'skip');
        inboxItems = inboxItems.filter((i) => i.speaker_uuid !== speaker_uuid);
        inboxTotal = Math.max(0, inboxTotal - 1);
      }
    } catch {
      toastStore.error($t('speakers.error.verify'));
    } finally {
      inboxActionInProgress.delete(speaker_uuid);
      inboxActionInProgress = inboxActionInProgress;
    }
  }

  // --- Search debounce ---

  function handleClusterSearch() {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      clusterPage = 1;
      loadClusters();
    }, 300);
  }

  // --- Keyboard shortcuts ---

  function handleKeydown(e: KeyboardEvent) {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;

    // Escape cancels merge/split mode from any tab
    if (e.key === 'Escape') {
      if (mergeMode) { cancelMerge(); return; }
      if (splitMode) { cancelSplit(); return; }
      if (unassignMode) { cancelUnassign(); return; }
      if (speakerPreviewData) { closeSpeakerPreview(); return; }
    }

    if (activeTab !== 'inbox' || !inboxItems.length) return;

    const first = inboxItems[0];
    if ((e.key === 'a' || e.key === 'A') && first?.suggested_name) {
      handleInboxAction(
        new CustomEvent('action', { detail: { type: 'accept', speaker_uuid: first.speaker_uuid } })
      );
    } else if (e.key === 's' || e.key === 'S' || e.key === 'n' || e.key === 'N') {
      if (first) {
        handleInboxAction(
          new CustomEvent('action', { detail: { type: 'skip', speaker_uuid: first.speaker_uuid } })
        );
      }
    } else if (e.key === 'p' || e.key === 'P') {
      if (first) openSpeakerPreview(first.speaker_uuid);
    }
  }
</script>

<svelte:head>
  <title>{$t('speakers.title')} - OpenTranscribe</title>
</svelte:head>

<svelte:window on:keydown={handleKeydown} />

<div class="speakers-page">
  <div class="page-header">
    <a href="/" class="back-to-gallery" title={$t('nav.backToGallery')}>
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="15 18 9 12 15 6"></polyline>
      </svg>
    </a>
    <h1>{$t('speakers.title')}</h1>
  </div>

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
    <button class="tab" class:active={activeTab === 'inbox'} on:click={() => switchTab('inbox')}>
      {$t('speakers.tabs.inbox')}
      {#if inboxTotal > 0}
        <span class="badge alert">{inboxTotal}</span>
      {/if}
    </button>
  </div>

  <!-- Clusters Tab -->
  {#if activeTab === 'clusters'}
    <div class="tab-content">
      <div class="toolbar">
        <div class="search-input-wrapper">
          <input
            type="text"
            class="search-input"
            placeholder={$t('speakers.searchPlaceholder')}
            bind:value={clusterSearch}
            on:input={handleClusterSearch}
          />
          {#if clusterSearch}
            <button
              class="search-clear-btn"
              on:click={() => { clusterSearch = ''; handleClusterSearch(); }}
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
        <button class="btn-recluster" on:click={handleRecluster} disabled={reclustering} title={$t('speakers.tooltip.recluster')}>
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
            <button class="btn-cancel-merge" on:click={cancelMerge}>{$t('modal.cancel')}</button>
          </div>
        {/if}

        <div class="cluster-list">
          {#if labeledCount > 0}
            <button class="section-heading-btn identified" on:click={() => toggleSection('identified')} title={identifiedCollapsed ? $t('speakers.tooltip.expandSection') : $t('speakers.tooltip.collapseSection')}>
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
                      on:expand={handleClusterExpand}
                      on:update={handleClusterUpdate}
                      on:promote={handleClusterPromote}
                      on:delete={handleClusterDelete}
                      on:merge={handleClusterMerge}
                      on:split={handleClusterSplit}
                      on:unassign={handleClusterUnassign}
                      on:cancelUnassign={cancelUnassign}
                      on:confirmUnassign={confirmUnassign}
                      on:toggleBlacklist={(e) => { unassignBlacklist = e.detail; }}
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
                            on:preview={(e) => openSpeakerPreview(e.detail.speaker_uuid)}
                            on:prefetch={(e) => prefetchSpeakerPreview(e.detail.speaker_uuid)}
                            on:toggleSplitMember={(e) => toggleSplitMember(e.detail)}
                            on:toggleUnassignMember={(e) => toggleUnassignMember(e.detail)}
                            on:cancelSplit={cancelSplit}
                            on:confirmSplit={confirmSplit}
                            on:outlierAnalysisComplete={handleOutlierAnalysisComplete}
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
            <button class="section-heading-btn unidentified" on:click={() => toggleSection('unidentified')} title={unidentifiedCollapsed ? $t('speakers.tooltip.expandSection') : $t('speakers.tooltip.collapseSection')}>
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
                      on:expand={handleClusterExpand}
                      on:update={handleClusterUpdate}
                      on:promote={handleClusterPromote}
                      on:delete={handleClusterDelete}
                      on:merge={handleClusterMerge}
                      on:split={handleClusterSplit}
                      on:unassign={handleClusterUnassign}
                      on:cancelUnassign={cancelUnassign}
                      on:confirmUnassign={confirmUnassign}
                      on:toggleBlacklist={(e) => { unassignBlacklist = e.detail; }}
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
                            on:preview={(e) => openSpeakerPreview(e.detail.speaker_uuid)}
                            on:prefetch={(e) => prefetchSpeakerPreview(e.detail.speaker_uuid)}
                            on:toggleSplitMember={(e) => toggleSplitMember(e.detail)}
                            on:toggleUnassignMember={(e) => toggleUnassignMember(e.detail)}
                            on:cancelSplit={cancelSplit}
                            on:confirmSplit={confirmSplit}
                            on:outlierAnalysisComplete={handleOutlierAnalysisComplete}
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
            <button disabled={clusterPage <= 1} on:click={() => { clusterPage--; loadClusters(); }}>
              {$t('speakers.pagination.prev')}
            </button>
            <span>{$t('speakers.pagination.pageOf', { page: clusterPage, pages: clusterPages })}</span>
            <button disabled={clusterPage >= clusterPages} on:click={() => { clusterPage++; loadClusters(); }}>
              {$t('speakers.pagination.next')}
            </button>
          </div>
        {/if}
      {/if}
    </div>
  {/if}

  <!-- Profiles Tab -->
  {#if activeTab === 'profiles'}
    <div class="tab-content">
      {#if loadingProfiles}
        <CardGridSkeleton variant="profile" count={8} minCardWidth={280} />
      {:else if profiles.length === 0}
        <EmptyState title={$t('speakers.profiles.emptyTitle')} description={$t('speakers.profiles.emptyDesc')} padding="60px 20px">
          <svelte:fragment slot="icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" />
            </svg>
          </svelte:fragment>
        </EmptyState>
      {:else}
        <div class="profile-list">
          {#each profiles as profile (profile.uuid)}
            <div class="profile-card">
              <div class="profile-header">
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <div class="profile-avatar-wrapper" on:click|stopPropagation={() => { if (!avatarUploading.has(profile.uuid)) { const el = document.getElementById('avatar-input-' + profile.uuid); el?.click(); } }} title={$t('speakers.tooltip.uploadAvatar')}>
                  {#if avatarUploading.has(profile.uuid)}
                    <div class="avatar-spinner"><Spinner size="small" /></div>
                  {:else if profile.avatar_url}
                    <img
                      class="profile-avatar"
                      src={profile.avatar_url}
                      alt={profile.name || 'Speaker avatar'}
                      loading="lazy"
                      decoding="async"
                    />
                    <div class="avatar-overlay">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                    </div>
                  {:else}
                    <div class="avatar-initials">{getInitials(profile.name || '?')}</div>
                    <div class="avatar-overlay">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                    </div>
                  {/if}
                  <input id="avatar-input-{profile.uuid}" type="file" accept="image/jpeg,image/png,image/gif,image/webp" style="display:none" on:change={(e) => handleAvatarUpload(profile.uuid, e)} />
                </div>
                {#if editingProfileUuid === profile.uuid}
                  <!-- svelte-ignore a11y-autofocus -->
                  <input class="profile-edit-input" bind:value={editProfileName}
                    on:keydown={(e) => { if (e.key === 'Enter') saveProfile(profile.uuid); if (e.key === 'Escape') cancelEditProfile(); }}
                    on:blur={() => saveProfile(profile.uuid)}
                    autofocus />
                {:else}
                  <div class="profile-name">{profile.name || $t('speakers.profiles.unnamed')}</div>
                  {#if profile.is_shared}
                    <span class="shared-badge" title={profile.owner_name ? $t('speakers.profiles.sharedBy', { name: profile.owner_name }) : $t('speakers.profiles.shared')}>{$t('speakers.profiles.shared')}</span>
                  {/if}
                {/if}
                <div class="profile-actions">
                  {#if !profile.is_shared}
                    <button class="icon-btn" on:click={() => startEditProfile(profile)} title={$t('speakers.profiles.edit')}>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                      </svg>
                    </button>
                    <button class="icon-btn danger" on:click={() => confirmDeleteProfile(profile.uuid)} title={$t('speakers.profiles.deleteBtn')}>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="3 6 5 6 21 6" />
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                      </svg>
                    </button>
                  {/if}
                </div>
              </div>
              {#if profile.description}
                <div class="profile-desc">{profile.description}</div>
              {/if}
              <div class="profile-meta">
                <span class="meta-stat" title="{$t('speakers.profiles.instances')}">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>
                  {profile.instance_count || 0}
                </span>
                <span class="meta-stat" title="{$t('speakers.profiles.files')}">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                  {profile.media_count || 0}
                </span>
                <span class="gender-confirm-group">
                  <button
                    class="gender-toggle-btn"
                    class:active={profile.predicted_gender === 'male'}
                    on:click|stopPropagation={() => handleConfirmProfileGender(profile, 'male')}
                    title={$t('speakers.profiles.confirmMale')}
                  >
                    <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="14" r="7"/><line x1="15" y1="9" x2="21" y2="3"/><polyline points="15 3 21 3 21 9"/></svg>{#if profile.predicted_gender === 'male' && genderConfirmedProfiles.has(profile.uuid)}<span class="gender-confirmed-tick">{'\u2713'}</span>{/if}
                  </button>
                  <button
                    class="gender-toggle-btn"
                    class:active={profile.predicted_gender === 'female'}
                    on:click|stopPropagation={() => handleConfirmProfileGender(profile, 'female')}
                    title={$t('speakers.profiles.confirmFemale')}
                  >
                    <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="9" r="7"/><line x1="12" y1="16" x2="12" y2="23"/><line x1="9" y1="20" x2="15" y2="20"/></svg>{#if profile.predicted_gender === 'female' && genderConfirmedProfiles.has(profile.uuid)}<span class="gender-confirmed-tick">{'\u2713'}</span>{/if}
                  </button>
                </span>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}

  <!-- Inbox Tab -->
  {#if activeTab === 'inbox'}
    <div class="tab-content">
      <div class="inbox-hint">
        {$t('speakers.keyboard.hint')}
      </div>
      {#if loadingInbox}
        <ListRowSkeleton count={5} size="compact" />
      {:else if inboxItems.length === 0}
        <EmptyState title={$t('speakers.inbox.emptyTitle')} description={profiles.length > 0 ? $t('speakers.inbox.emptyAllVerified') : $t('speakers.inbox.emptyNoProfiles')} padding="60px 20px">
          <svelte:fragment slot="icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color: var(--success-color, #059669); opacity: 0.7;">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
            </svg>
          </svelte:fragment>
        </EmptyState>
      {:else}
        <div class="inbox-list">
          {#each inboxItems as item, idx (item.speaker_uuid)}
            <SpeakerInboxItem
              {item}
              actionInProgress={inboxActionInProgress.has(item.speaker_uuid)}
              on:action={handleInboxAction}
              on:preview={(e) => openSpeakerPreview(e.detail.speaker_uuid)}
              on:prefetch={(e) => prefetchSpeakerPreview(e.detail.speaker_uuid)}
            />
          {/each}
        </div>

        {#if inboxPages > 1}
          <div class="pagination">
            <button disabled={inboxPage <= 1} on:click={() => { inboxPage--; loadInbox(); }}>
              {$t('speakers.pagination.prev')}
            </button>
            <span>{$t('speakers.pagination.pageOf', { page: inboxPage, pages: inboxPages })}</span>
            <button disabled={inboxPage >= inboxPages} on:click={() => { inboxPage++; loadInbox(); }}>
              {$t('speakers.pagination.next')}
            </button>
          </div>
        {/if}
      {/if}
    </div>
  {/if}

  <!-- Delete Cluster Confirmation Modal -->
  <ConfirmationModal
    isOpen={showDeleteModal}
    title={$t('speakers.delete.title')}
    message={$t('speakers.delete.message')}
    confirmText={$t('modal.delete')}
    confirmButtonClass="confirm-button delete-confirm"
    on:confirm={confirmDelete}
    on:cancel={() => { showDeleteModal = false; }}
    on:close={() => { showDeleteModal = false; }}
  />

  <!-- Delete Profile Confirmation Modal -->
  <ConfirmationModal
    isOpen={showDeleteProfileModal}
    title={$t('speakers.profiles.deleteTitle')}
    message={$t('speakers.profiles.deleteMessage')}
    confirmText={$t('modal.delete')}
    confirmButtonClass="confirm-button delete-confirm"
    on:confirm={handleDeleteProfile}
    on:cancel={() => { showDeleteProfileModal = false; }}
    on:close={() => { showDeleteProfileModal = false; }}
  />

  <!-- Promote Modal (inline with text input) -->
  {#if showPromoteModal}
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <div class="modal-backdrop" on:click|self={() => { showPromoteModal = false; }} on:wheel|preventDefault|self on:touchmove|preventDefault|self transition:fade={{ duration: 200 }}>
      <div class="promote-modal" transition:fade={{ duration: 200, delay: 100 }}>
        <h3>{$t('speakers.promote.title')}</h3>
        <p>{$t('speakers.promote.description')}</p>
        <!-- svelte-ignore a11y-autofocus -->
        <input
          class="promote-modal-input"
          bind:value={promoteNameInput}
          placeholder={$t('speakers.promote.namePlaceholder')}
          on:keydown={(e) => { if (e.key === 'Enter') confirmPromote(); if (e.key === 'Escape') { showPromoteModal = false; } }}
          autofocus
        />
        <div class="promote-modal-actions">
          <button class="btn-cancel" on:click={() => { showPromoteModal = false; }}>{$t('modal.cancel')}</button>
          <button class="btn-confirm" on:click={confirmPromote} disabled={!promoteNameInput.trim()}>{$t('speakers.promote.confirm')}</button>
        </div>
      </div>
    </div>
  {/if}
</div>

<!-- Sticky Floating Preview Player -->
{#if speakerPreviewData}
  <div class="sticky-preview" transition:fade={{ duration: 150 }}>
    <div class="preview-header">
      <div class="preview-info">
        <span class="preview-title">
          {speakerPreviewData.speaker_name}
        </span>
        <span class="preview-playback-info">
          <span class="preview-time">{formatPlaybackTime(previewCurrentTime)}</span>
          <span class="preview-separator">|</span>
          <span class="preview-file-name">{speakerPreviewData.file_name}</span>
        </span>
      </div>
      <div class="preview-actions">
        <a class="preview-detail-link" href="/files/{speakerPreviewData.file_uuid}?t={previewCurrentTime || speakerPreviewData.start_time}">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
            <polyline points="15 3 21 3 21 9"></polyline>
            <line x1="10" y1="14" x2="21" y2="3"></line>
          </svg>
          {$t('search.jumpTo')}
        </a>
        <button class="preview-close" on:click={closeSpeakerPreview} title={$t('speakers.preview.close')} aria-label={$t('speakers.preview.closeAriaLabel')}>
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
    </div>
    <div class="preview-player-container">
      {#key `${speakerPreviewData?.media_url}:${speakerPreviewData?.start_time}`}
        {#if PlyrMiniPlayer && speakerPreviewData}
          <svelte:component this={PlyrMiniPlayer}
            bind:this={previewPlayerRef}
            mediaUrl={speakerPreviewData.media_url || ''}
            contentType={speakerPreviewData.content_type}
            startTime={speakerPreviewData.start_time}
            endTime={speakerPreviewData.end_time}
            autoplay={true}
            fileId={speakerPreviewData.file_uuid}
            compact={true}
            on:timeupdate={handlePreviewTimeUpdate}
            on:play={handlePreviewPlay}
            on:pause={handlePreviewPause}
          />
        {/if}
      {/key}
    </div>
  </div>
{/if}

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
    background: var(--hover-color, rgba(0, 0, 0, 0.05));
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
    color: var(--text-secondary, #6b7280);
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
    color: var(--text-color, #111827);
    background: none;
    transform: none;
    box-shadow: none;
  }

  .tab.active {
    color: var(--primary-color);
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
    background: var(--error-color, #ef4444);
    color: white;
  }

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

  /* member-list styles moved to ClusterMemberList.svelte */

  .gender-confirm-group {
    display: inline-flex;
    gap: 4px;
    align-items: center;
  }

  .gender-toggle-btn {
    font-size: 14px;
    padding: 3px 6px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #d1d5db);
    background: transparent;
    color: var(--text-secondary, #9ca3af);
    cursor: pointer;
    transition: all 0.15s ease;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    gap: 2px;
  }

  .gender-svg {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }

  .gender-toggle-btn:hover {
    border-color: var(--primary-color, #3b82f6);
    color: var(--primary-color, #3b82f6);
  }

  .gender-toggle-btn.active {
    border-color: var(--primary-color, #3b82f6);
    background: color-mix(in srgb, var(--primary-color, #3b82f6) 12%, transparent);
    color: var(--primary-color, #3b82f6);
  }

  .loading-members {
    padding: 12px;
    color: var(--text-secondary);
    font-size: 13px;
  }

  .profile-list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
    max-width: 100%;
  }

  .profile-card {
    padding: 16px;
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 8px;
    background: var(--card-background, #fff);
    transition: box-shadow 0.15s ease;
    overflow: hidden;
    min-width: 0;
  }

  .profile-card:hover {
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  .profile-avatar-wrapper {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    flex-shrink: 0;
    cursor: pointer;
    position: relative;
    overflow: hidden;
  }

  .profile-avatar-wrapper:hover .avatar-overlay {
    opacity: 1;
  }

  .profile-avatar {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    object-fit: cover;
  }

  .avatar-initials {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 16px;
    font-weight: 600;
    color: var(--primary-color, #3b82f6);
    background: color-mix(in srgb, var(--primary-color, #3b82f6) 20%, transparent);
  }

  .avatar-overlay {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    background: rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    transition: opacity 0.15s ease;
    color: white;
  }

  .avatar-spinner {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    background: color-mix(in srgb, var(--primary-color, #3b82f6) 20%, transparent);
  }

  .profile-header {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }

  .profile-name {
    font-weight: 600;
    font-size: 15px;
    color: var(--text-color);
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .profile-desc {
    margin-top: 4px;
    font-size: 13px;
    color: var(--text-secondary);
    word-break: break-word;
    overflow-wrap: break-word;
  }

  .profile-meta {
    margin-top: 8px;
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    color: var(--text-secondary);
    flex-wrap: wrap;
  }

  .meta-stat {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-weight: 500;
    cursor: default;
  }

  .meta-stat svg {
    opacity: 0.6;
    flex-shrink: 0;
  }


  .inbox-hint {
    padding: 8px 16px;
    background: var(--hover-color);
    border-radius: 6px;
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 12px;
  }

  .inbox-list {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
    max-width: 100%;
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

  /* Promote modal */
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--modal-backdrop, rgba(0, 0, 0, 0.5));
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1300;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .promote-modal {
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 24px;
    max-width: 400px;
    width: 90%;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1);
  }

  .promote-modal h3 {
    margin: 0 0 8px;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-color);
  }

  .promote-modal p {
    margin: 0 0 16px;
    font-size: 14px;
    color: var(--text-secondary);
  }

  .promote-modal-input {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--input-border);
    border-radius: 8px;
    background: var(--input-background);
    color: var(--text-color);
    font-size: 14px;
    margin-bottom: 8px;
    box-sizing: border-box;
  }

  .promote-modal-input:focus {
    outline: none;
    border-color: var(--input-focus-border);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-color, #3b82f6) 10%, transparent);
  }

  .promote-modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 16px;
  }

  .btn-cancel {
    padding: 8px 16px;
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 8px;
    background: var(--card-background, #fff);
    color: var(--text-color, #111827);
    cursor: pointer;
    font-size: 14px;
    box-shadow: none;
  }

  .btn-cancel:hover {
    background: var(--hover-color, #f3f4f6);
    transform: none;
    box-shadow: none;
  }

  .btn-confirm {
    padding: 8px 16px;
    border: none;
    border-radius: 8px;
    background: var(--primary-color, #3b82f6);
    color: white;
    cursor: pointer;
    font-size: 14px;
    box-shadow: none;
  }

  .btn-confirm:hover:not(:disabled) {
    background: var(--primary-hover, #2563eb);
    transform: none;
    box-shadow: none;
  }

  .btn-confirm:disabled {
    opacity: 0.5;
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

  /* split/unassign styles moved to ClusterMemberList.svelte */


  .profile-actions {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    min-width: 28px;
    padding: 0;
    border-radius: 6px;
    border: 1px solid var(--border-color, #e5e7eb);
    background: var(--card-background, #fff);
    color: var(--text-secondary, #6b7280);
    cursor: pointer;
    transition: all 0.15s ease;
    box-shadow: none;
    font-size: 0;
  }

  .icon-btn:hover {
    background: var(--hover-color, #f3f4f6);
    color: var(--text-color, #111827);
    transform: none;
    box-shadow: none;
  }

  .icon-btn.danger:hover {
    color: var(--error-color, #ef4444);
    background: color-mix(in srgb, var(--error-color, #ef4444) 10%, transparent);
    border-color: color-mix(in srgb, var(--error-color, #ef4444) 30%, transparent);
    transform: none;
    box-shadow: none;
  }

  .profile-edit-input {
    flex: 1;
    min-width: 0;
    padding: 4px 8px;
    border: 2px solid var(--primary-color, #3b82f6);
    border-radius: 6px;
    background: var(--input-background, #fff);
    color: var(--text-color, #111827);
    font-size: 15px;
    font-weight: 600;
    outline: none;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-color, #3b82f6) 10%, transparent);
    box-sizing: border-box;
  }

  /* member-play-btn styles moved to ClusterMemberList.svelte */

  /* Sticky Floating Preview Player */
  .sticky-preview {
    position: fixed;
    bottom: 1rem;
    right: 1rem;
    width: 400px;
    max-width: calc(100vw - 2rem);
    background: var(--surface-color, #fff);
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.15), 0 2px 8px rgba(0, 0, 0, 0.08);
    z-index: 1000;
    overflow: hidden;
  }

  .preview-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.5rem 0.75rem;
    background: var(--surface-color, #f9fafb);
    border-bottom: 1px solid var(--border-color, #e5e7eb);
  }

  .preview-info {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    min-width: 0;
    flex: 1;
  }

  .preview-title {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color, #111827);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .preview-playback-info {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    font-size: 0.75rem;
  }

  .preview-time {
    font-family: monospace;
    font-weight: 600;
    color: var(--primary-color, #4f46e5);
  }

  .preview-separator {
    color: var(--text-secondary, #9ca3af);
  }

  .preview-file-name {
    color: var(--text-secondary, #6b7280);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .preview-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
    margin-left: 0.5rem;
  }

  .preview-detail-link {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    font-size: 0.75rem;
    color: var(--primary-color, #4f46e5);
    text-decoration: none;
    padding: 0.25rem 0.5rem;
    border-radius: 6px;
    transition: background 0.15s;
  }

  .preview-detail-link:hover {
    background: color-mix(in srgb, var(--primary-color, #4f46e5) 8%, transparent);
  }

  .preview-close {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
    transition: color 0.2s ease, background 0.2s ease;
  }

  .preview-close:hover {
    color: var(--text-color);
    background: var(--button-hover, var(--background-color));
  }

  .shared-badge {
    display: inline-block;
    font-size: 0.65rem;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--accent-light, #e0e7ff);
    color: var(--accent, #4f46e5);
    margin-left: 6px;
    vertical-align: middle;
    white-space: nowrap;
  }
  :global(.dark) .shared-badge {
    background: rgba(99, 102, 241, 0.2);
    color: #a5b4fc;
  }

  /* Raise modal above navbar (z-index 1200) on tablet/mobile */

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

    .profile-list {
      grid-template-columns: 1fr;
      gap: 10px;
    }

    .profile-card {
      padding: 12px;
    }

    .profile-header {
      gap: 8px;
    }

    .profile-avatar-wrapper,
    .profile-avatar,
    .avatar-initials,
    .avatar-spinner {
      width: 40px;
      height: 40px;
    }

    .avatar-initials {
      font-size: 14px;
    }

    .profile-name {
      font-size: 14px;
    }

    .profile-meta {
      gap: 8px;
    }

    .inbox-hint {
      padding: 6px 12px;
      font-size: 12px;
    }

    .pagination {
      gap: 8px;
      font-size: 13px;
    }

    .pagination button {
      padding: 6px 10px;
      font-size: 13px;
    }

    .sticky-preview {
      left: 0.5rem;
      right: 0.5rem;
      bottom: 0.5rem;
      width: auto;
      max-width: none;
    }

    .preview-header {
      padding: 0.375rem 0.5rem;
    }

    .preview-detail-link {
      font-size: 0.6875rem;
      padding: 0.25rem 0.375rem;
    }

    .promote-modal {
      padding: 16px;
      width: 92%;
    }
  }
</style>
