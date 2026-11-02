<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import ClustersTab from '$components/speakers/ClustersTab.svelte';
  import SpeakerPromoteModal from '$components/speakers/SpeakerPromoteModal.svelte';
  import {
    getClusterDetail,
    updateCluster,
    deleteCluster,
    promoteCluster,
    mergeClusters,
    splitCluster,
    triggerRecluster,
    unassignSpeakers
  } from '$lib/api/speakerClusters';
  import type { SpeakerCluster, SpeakerClusterMember } from '$lib/types/speakerCluster';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import { fetchClusterPage } from '$lib/speakers/loaders';
  import {
    clampPageAfterRemoval,
    loadCollapseState,
    saveCollapseState,
    toggleMember
  } from '$lib/speakers/pageHelpers';

  /**
   * Everything the Clusters tab does: the list, merge / split / unassign / promote / delete
   * flows, reclustering and its websocket progress. The page keeps the tab bar and the other
   * tabs; this stays mounted across tab switches so its state (search, page, expanded card)
   * survives them, and only renders the tab itself while `active`.
   */
  export let active = false;
  export let reviewCount = 0;
  /** Bound by the page for the tab badge. */
  export let clusterTotal = 0;
  export let onRenameStarted: () => void = () => {};
  export let onPreview: (speakerUuid: string) => void = () => {};
  export let onPrefetch: (speakerUuid: string) => void = () => {};
  export let onOpenReview: () => void = () => {};

  let clusters: SpeakerCluster[] = [];
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

  let identifiedCollapsed = false;
  let unidentifiedCollapsed = false;

  $: identifiedClusters = clusters.filter(c => c.promoted_to_profile_name || c.label);
  $: unidentifiedClusters = clusters.filter(c => !(c.promoted_to_profile_name || c.label));

  let mergeMode = false;
  let mergeSourceUuid: string | null = null;

  let splitMode = false;
  let splitTargetUuid: string | null = null;
  let splitSelectedMembers: Set<string> = new Set();

  let unassignMode = false;
  let unassignTargetUuid: string | null = null;
  let unassignSelectedMembers: Set<string> = new Set();
  let unassignBlacklist = true;

  // Outlier data per cluster (populated by ClusterMemberList embedding analysis)
  let clusterOutlierData: Record<string, { outlierUuids: string[] }> = {};

  let showDeleteModal = false;
  let deleteTargetUuid = '';

  let showPromoteModal = false;
  let promoteTargetUuid = '';
  let promoteNameInput = '';

  let clusteringProgress: { step: number; total_steps: number; message: string; progress: number } | null = null;

  const debouncedClusterSearch = createDebouncedHandler(() => {
    clusterPage = 1;
    load();
  }, 300);

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
    load();
    toastStore.success($t('speakers.reclusterComplete'));
  }

  function handleClusteringFileComplete() {
    if (active) load();
  }

  onMount(() => {
    // Restore collapse state from localStorage
    const saved = loadCollapseState();
    identifiedCollapsed = saved.identified;
    unidentifiedCollapsed = saved.unidentified;

    window.addEventListener('clustering-progress', handleClusteringProgress);
    window.addEventListener('clustering-complete', handleClusteringComplete);
    window.addEventListener('clustering-file-complete', handleClusteringFileComplete);
  });

  onDestroy(() => {
    window.removeEventListener('clustering-progress', handleClusteringProgress);
    window.removeEventListener('clustering-complete', handleClusteringComplete);
    window.removeEventListener('clustering-file-complete', handleClusteringFileComplete);
    debouncedClusterSearch.cleanup();
    if (reclusterTimeout) clearTimeout(reclusterTimeout);
  });

  function toggleSection(section: 'identified' | 'unidentified') {
    if (section === 'identified') identifiedCollapsed = !identifiedCollapsed;
    else unidentifiedCollapsed = !unidentifiedCollapsed;
    saveCollapseState({ identified: identifiedCollapsed, unidentified: unidentifiedCollapsed });
  }

  // --- Page-facing API ---

  export async function load(silent = false) {
    loadingClusters = true;
    try {
      const res = await fetchClusterPage(clusterPage, clusterSearch);
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

  /** Leave merge / split / unassign mode (the page calls this when the tab changes). */
  export function resetModes() {
    mergeMode = false;
    mergeSourceUuid = null;
    cancelSplit();
    cancelUnassign();
  }

  /** Escape: cancel whichever mode is active. Returns whether one was. */
  export function cancelActiveMode(): boolean {
    if (mergeMode) { cancelMerge(); return true; }
    if (splitMode) { cancelSplit(); return true; }
    if (unassignMode) { cancelUnassign(); return true; }
    return false;
  }

  // --- Cluster actions ---

  /** Fetch a cluster's members once; false if the fetch failed. */
  async function ensureMembers(uuid: string): Promise<boolean> {
    if (clusterMembers[uuid]) return true;
    try {
      const detail = await getClusterDetail(uuid);
      clusterMembers[uuid] = detail.members;
      clusterMembers = clusterMembers;
      return true;
    } catch {
      return false;
    }
  }

  async function handleClusterExpand(e: CustomEvent<{ uuid: string }>) {
    const uuid = e.detail.uuid;
    expandedCluster = uuid;
    await ensureMembers(uuid); // silently fail
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
      // which queues digest regeneration server-side.
      onRenameStarted();
    } catch {
      toastStore.error($t('speakers.error.updateCluster'));
    }
  }

  async function handleClusterDelete(e: CustomEvent<{ uuid: string }>) {
    deleteTargetUuid = e.detail.uuid;
    showDeleteModal = true;
  }

  async function confirmDelete() {
    try {
      await deleteCluster(deleteTargetUuid);
      toastStore.success($t('speakers.cluster.deleted'));
      clusterPage = clampPageAfterRemoval(clusterPage, clusterTotal, 1);
      await load();
    } catch {
      toastStore.error($t('speakers.error.delete'));
    }
    showDeleteModal = false;
    deleteTargetUuid = '';
  }

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
      onRenameStarted();
      await load();
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
        const merged = await mergeClusters(mergeSourceUuid, targetUuid);
        const leftBehind = merged.blocked_speaker_uuids?.length ?? 0;
        if (leftBehind > 0) {
          // The merge honoured earlier corrections and says so (#1233): those speakers
          // stay in the source cluster rather than silently joining the target.
          toastStore.warning($t('speakers.merge.someLeftBehind', { count: leftBehind }));
        } else {
          toastStore.success($t('speakers.merge.success'));
        }
        mergeMode = false;
        mergeSourceUuid = null;
        // the source cluster is absorbed into the target
        clusterPage = clampPageAfterRemoval(clusterPage, clusterTotal, 1);
        await load();
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

  /** Expand both sections and the cluster so its members can be picked from. */
  async function openForSelection(uuid: string): Promise<boolean> {
    identifiedCollapsed = false;
    unidentifiedCollapsed = false;
    if (expandedCluster !== uuid) {
      expandedCluster = uuid;
      if (!(await ensureMembers(uuid))) {
        toastStore.error($t('speakers.error.loadClusters'));
        return false;
      }
    }
    return true;
  }

  // Split flow
  async function handleClusterSplit(e: CustomEvent<{ uuid: string }>) {
    const uuid = e.detail.uuid;
    if (!(await openForSelection(uuid))) return;
    splitTargetUuid = uuid;
    splitMode = true;
    splitSelectedMembers = new Set();
  }

  async function confirmSplit() {
    if (!splitTargetUuid || splitSelectedMembers.size === 0) return;
    try {
      await splitCluster(splitTargetUuid, Array.from(splitSelectedMembers));
      toastStore.success($t('speakers.split.success'));
      cancelSplit();
      await load();
    } catch {
      cancelSplit();
      toastStore.error($t('speakers.split.error'));
    }
  }

  function cancelSplit() {
    splitMode = false;
    splitTargetUuid = null;
    splitSelectedMembers = new Set();
  }

  // Unassign flow
  async function handleClusterUnassign(e: CustomEvent<{ uuid: string }>) {
    const uuid = e.detail.uuid;
    if (!(await openForSelection(uuid))) return;
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
      cancelUnassign();
      await load();
    } catch {
      cancelUnassign();
      toastStore.error($t('speakers.split.error'));
    }
  }

  function cancelUnassign() {
    unassignMode = false;
    unassignTargetUuid = null;
    unassignSelectedMembers = new Set();
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
</script>

{#if active}
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
    {reviewCount}
    on:openReview={onOpenReview}
    on:search={() => debouncedClusterSearch.trigger()}
    on:recluster={handleRecluster}
    on:toggleSection={(e) => toggleSection(e.detail)}
    on:cancelMerge={cancelMerge}
    on:prevPage={() => { clusterPage--; load(); }}
    on:nextPage={() => { clusterPage++; load(); }}
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
    on:preview={(e) => onPreview(e.detail.speaker_uuid)}
    on:prefetch={(e) => onPrefetch(e.detail.speaker_uuid)}
    on:toggleSplitMember={(e) => { splitSelectedMembers = toggleMember(splitSelectedMembers, e.detail); }}
    on:toggleUnassignMember={(e) => { unassignSelectedMembers = toggleMember(unassignSelectedMembers, e.detail); }}
    on:cancelSplit={cancelSplit}
    on:confirmSplit={confirmSplit}
    on:outlierAnalysisComplete={handleOutlierAnalysisComplete}
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

<SpeakerPromoteModal
  isOpen={showPromoteModal}
  bind:name={promoteNameInput}
  on:confirm={confirmPromote}
  on:close={() => { showPromoteModal = false; }}
/>
