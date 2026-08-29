<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { fade } from 'svelte/transition';
  import { page } from '$app/stores';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import ClustersTab from '$components/speakers/ClustersTab.svelte';
  import ProfilesTab from '$components/speakers/ProfilesTab.svelte';
  import InboxTab from '$components/speakers/InboxTab.svelte';
  import SpeakerPreviewPlayer from '$components/speakers/SpeakerPreviewPlayer.svelte';
  import { audioPlaybackStore } from '$stores/audioPlaybackStore';
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
    unassignSpeakers,
    RENAME_PROPAGATION_TIMEOUT_MS
  } from '$lib/api/speakerClusters';
  import type { SpeakerMediaPreviewData } from '$lib/api/speakerClusters';
  import type {
    SpeakerCluster,
    SpeakerClusterMember,
    SpeakerInboxItem as InboxItem,
    SpeakerProfile
  } from '$lib/types/speakerCluster';
  import { apiCache } from '$lib/apiCache';
  import { createDebouncedHandler } from '$lib/utils/debounce';

  type Tab = 'clusters' | 'profiles' | 'inbox';
  // Deep link from elsewhere in the app — currently the file-detail speaker
  // list's "has a profile" badge (`transcript/SpeakerEditorPanel.svelte`),
  // which links here as `/speakers?tab=profiles` so a labeled speaker's
  // profile is one click from where it was noticed rather than a manual
  // tab switch + hunt.
  const initialTab = $page.url.searchParams.get('tab');
  let activeTab: Tab = initialTab === 'profiles' || initialTab === 'inbox' ? initialTab : 'clusters';

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
  const debouncedClusterSearch = createDebouncedHandler(() => {
    clusterPage = 1;
    loadClusters();
  }, 300);

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

  // Rename-propagation progress (via WebSocket, issue W2.3a).
  //
  // Every rename here — a cluster label edit, promoting a cluster to a
  // profile, or accepting an inbox suggestion — assigns Speaker.display_name
  // through SpeakerClusteringService, which batches its renames through
  // SpeakerRenameTracker and flushes via dispatch_speaker_rename. That now
  // also queues per-file digest regeneration (backend/app/tasks/
  // rename_propagation_task.py), reported back as a single
  // `speaker_rename_propagation` WebSocket message per batch — there is no
  // "started" frame, only "completed", so this counter is bumped optimistically
  // right after the triggering action succeeds and cleared by the completion
  // event (or a timeout, in case the event never arrives — a stopped worker
  // must not leave the indicator stuck forever).
  let pendingRenamePropagations = 0;
  let renamePropagationTimers: ReturnType<typeof setTimeout>[] = [];

  function beginRenamePropagation() {
    pendingRenamePropagations += 1;
    const timer = setTimeout(() => {
      pendingRenamePropagations = Math.max(0, pendingRenamePropagations - 1);
    }, RENAME_PROPAGATION_TIMEOUT_MS);
    renamePropagationTimers.push(timer);
  }

  function handleRenamePropagation(event: Event) {
    const detail = (event as CustomEvent).detail as
      | { new_name?: string; regenerated?: number; errors?: number; total?: number }
      | undefined;
    const timer = renamePropagationTimers.shift();
    if (timer) clearTimeout(timer);
    pendingRenamePropagations = Math.max(0, pendingRenamePropagations - 1);

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
    window.addEventListener('speaker-rename-propagation', handleRenamePropagation);
  });

  onDestroy(() => {
    window.removeEventListener('clustering-progress', handleClusteringProgress);
    window.removeEventListener('clustering-complete', handleClusteringComplete);
    window.removeEventListener('clustering-file-complete', handleClusteringFileComplete);
    window.removeEventListener('speaker-rename-propagation', handleRenamePropagation);
    debouncedClusterSearch.cleanup();
    if (reclusterTimeout) clearTimeout(reclusterTimeout);
    renamePropagationTimers.forEach(clearTimeout);
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
      const updated = await updateCluster(e.detail.uuid, { label: e.detail.label });
      // Patch the one card from the response. Reloading the page of clusters put the whole
      // grid back into its skeleton state for a single-field edit.
      clusters = clusters.map(cluster =>
        cluster.uuid === e.detail.uuid ? { ...cluster, ...updated } : cluster
      );
      // A label edit renames every speaker in the cluster (SpeakerClusteringService),
      // which queues digest regeneration server-side — see the state comment above.
      beginRenamePropagation();
    } catch {
      toastStore.error($t('speakers.error.updateCluster'));
    }
  }

  // Delete cluster (modal)
  async function handleClusterDelete(e: CustomEvent<{ uuid: string }>) {
    deleteTargetUuid = e.detail.uuid;
    showDeleteModal = true;
  }

  /**
   * Clamp the current page for a list that just lost `removed` entries.
   *
   * Do this BEFORE reloading: clamping afterwards meant deleting the last item on the last
   * page fetched that page, discovered it was now out of range, and fetched again —
   * two round trips and two skeleton flashes.
   */
  function clampClusterPage(removed: number) {
    const remaining = Math.max(0, clusterTotal - removed);
    const pages = Math.max(1, Math.ceil(remaining / 20));
    if (clusterPage > pages) clusterPage = pages;
  }

  async function confirmDelete() {
    try {
      await deleteCluster(deleteTargetUuid);
      toastStore.success($t('speakers.cluster.deleted'));
      clampClusterPage(1);
      await loadClusters();
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
      beginRenamePropagation();
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
        clampClusterPage(1); // the source cluster is absorbed into the target
        await loadClusters();
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
      const updated = await updateProfile(uuid, { name: editProfileName.trim() });
      // Patch from the response, as the avatar and gender handlers already do — reloading
      // the list skeletoned the whole Profiles tab for a one-field edit.
      profiles = profiles.map(profile =>
        profile.uuid === uuid ? { ...profile, ...updated } : profile
      );
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
        beginRenamePropagation();
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
    debouncedClusterSearch.trigger();
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
    {#if pendingRenamePropagations > 0}
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
    <ClustersTab
      bind:clusterSearch
      {reclustering}
      {lastClusteredAt}
      {clusteringProgress}
      {loadingClusters}
      {clusters}
      {identifiedClusters}
      {unidentifiedClusters}
      {labeledCount}
      {unlabeledCount}
      {identifiedCollapsed}
      {unidentifiedCollapsed}
      {mergeMode}
      {mergeSourceUuid}
      {expandedCluster}
      {clusterMembers}
      {splitMode}
      {splitTargetUuid}
      {splitSelectedMembers}
      {unassignMode}
      {unassignTargetUuid}
      {unassignSelectedMembers}
      {unassignBlacklist}
      {clusterPage}
      {clusterPages}
      on:search={handleClusterSearch}
      on:recluster={handleRecluster}
      on:toggleSection={(e) => toggleSection(e.detail)}
      on:cancelMerge={cancelMerge}
      on:prevPage={() => { clusterPage--; loadClusters(); }}
      on:nextPage={() => { clusterPage++; loadClusters(); }}
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
      on:preview={(e) => openSpeakerPreview(e.detail.speaker_uuid)}
      on:prefetch={(e) => prefetchSpeakerPreview(e.detail.speaker_uuid)}
      on:toggleSplitMember={(e) => toggleSplitMember(e.detail)}
      on:toggleUnassignMember={(e) => toggleUnassignMember(e.detail)}
      on:cancelSplit={cancelSplit}
      on:confirmSplit={confirmSplit}
      on:outlierAnalysisComplete={handleOutlierAnalysisComplete}
    />
  {/if}

  <!-- Profiles Tab -->
  {#if activeTab === 'profiles'}
    <ProfilesTab
      {loadingProfiles}
      {profiles}
      {avatarUploading}
      {editingProfileUuid}
      bind:editProfileName
      {genderConfirmedProfiles}
      on:avatarUpload={(e) => handleAvatarUpload(e.detail.uuid, e.detail.event)}
      on:startEdit={(e) => startEditProfile(e.detail)}
      on:saveProfile={(e) => saveProfile(e.detail)}
      on:cancelEdit={cancelEditProfile}
      on:confirmDelete={(e) => confirmDeleteProfile(e.detail)}
      on:confirmGender={(e) => handleConfirmProfileGender(e.detail.profile, e.detail.gender)}
    />
  {/if}

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
      on:prefetch={(e) => prefetchSpeakerPreview(e.detail.speaker_uuid)}
      on:prevPage={() => { inboxPage--; loadInbox(); }}
      on:nextPage={() => { inboxPage++; loadInbox(); }}
    />
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
    color: var(--primary-color);
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
    z-index: var(--z-modal);
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

    .promote-modal {
      padding: 16px;
      width: 92%;
    }
  }
</style>
