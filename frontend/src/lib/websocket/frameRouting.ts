/**
 * Side effects of an inbound realtime frame, before (or instead of) a notification.
 *
 * Settings panels and pages listen for window `CustomEvent`s rather than subscribing to
 * the store, and several frame types drop `apiCache` scopes so a list does not outlive a
 * change the server just announced. The event names are a contract with those listeners
 * (see `src/stores/CLAUDE.md`): keep them stable.
 */

interface FrameRoute {
  /** Window `CustomEvent` dispatched with the frame's `data` as `detail`. */
  event?: string;
  /** `apiCache` scopes dropped, in this order. */
  scopes?: readonly string[];
  /** The frame is fully handled here and creates no notification. */
  consumed?: boolean;
}

const SHARE_SCOPES = ['collections', 'files', 'shares', 'shared_collections'] as const;

/** Frame `type` to its side effects. A type absent here only produces a notification. */
const FRAME_ROUTES: Readonly<Record<string, FrameRoute>> = {
  // Connection confirmation and debug/heartbeat echoes: nothing to show.
  connection_established: { consumed: true },
  echo: { consumed: true },
  // Silent GPU stats update for SettingsModal.
  gpu_stats_update: { event: 'gpu-stats-updated', consumed: true },
  // Model-library progress (#1246), sent only to the admin who started it. The
  // library toasts on a finish itself; no bell entry per tick.
  local_model_download: { event: 'local-model-download', consumed: true },
  // Watch-source scan completed, for WatchSourcesSettings.
  watch_source_scan: { event: 'watch-source-scan', consumed: true },
  // Admin tasks: the settings panel follows the event, the bell gets a progress card.
  reindex_progress: { event: 'reindex-progress' },
  reindex_complete: { event: 'reindex-complete' },
  reindex_stopped: { event: 'reindex-stopped' },
  migration_progress: { event: 'migration-progress' },
  migration_complete: { event: 'migration-complete' },
  migration_finalized: { event: 'migration-finalized' },
  attribute_migration_progress: { event: 'attribute-migration-progress' },
  attribute_migration_complete: { event: 'attribute-migration-complete' },
  data_integrity_progress: { event: 'data-integrity-progress' },
  data_integrity_complete: { event: 'data-integrity-complete' },
  embedding_consistency_progress: { event: 'embedding-consistency-progress' },
  embedding_consistency_complete: { event: 'embedding-consistency-complete' },
  // Speaker attributes detected / background processing complete: refresh, no notification.
  speaker_updated: { event: 'speaker-updated', consumed: true },
  speaker_processing_complete: { event: 'speaker-processing-complete', consumed: true },
  // A rename's digest-plane regeneration finished (backend/app/tasks/
  // rename_propagation_task.py:regenerate_rename_digests). Without this, a user who
  // reopens a file's summary right after a rename keeps reading the pre-rename cached
  // copy for the rest of its TTL. 'files' covers `prefetch:file:`, the file-detail cache
  // that holds `summary_data`; there is no separate frontend cache for the digest tier.
  speaker_rename_propagation: {
    event: 'speaker-rename-propagation',
    scopes: ['files', 'speakers'],
    consumed: true,
  },
  // Speaker clustering, for SpeakersPage.
  clustering_progress: { event: 'clustering-progress' },
  clustering_complete: { event: 'clustering-complete' },
  clustering_file_complete: { event: 'clustering-file-complete', consumed: true },
  // Sharing and group membership: drop every cache that could list the collection,
  // then show a visible notification.
  collection_shared: { event: 'collection-shared', scopes: SHARE_SCOPES },
  collection_share_revoked: { event: 'share-revoked', scopes: SHARE_SCOPES },
  collection_share_updated: {
    event: 'share-updated',
    scopes: ['collections', 'shares', 'shared_collections'],
  },
  // A collection this user could see was restricted (issue #1275): the payload is the
  // collection uuid only.
  collection_access_changed: { event: 'collection-access-changed', scopes: SHARE_SCOPES },
  group_member_added: { event: 'group-member-added', scopes: SHARE_SCOPES },
  group_member_removed: { event: 'group-member-removed', scopes: SHARE_SCOPES },
  // DMCA takedown / release owner notice: the file's visibility just changed, so drop
  // cached file lists before the (persistent) notification.
  file_takedown: { scopes: ['files'] },
  file_takedown_released: { scopes: ['files'] },
  // For AutoLabelSettings; the progress card follows.
  auto_label_status: { event: 'auto-label-status' },
};

interface Frame {
  type?: unknown;
  data?: { scope?: string } & Record<string, unknown>;
}

function dispatch(name: string, detail: unknown): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(name, { detail }));
  }
}

function invalidate(scopes: readonly string[]): void {
  import('$lib/apiCache')
    .then(({ apiCache }) => {
      for (const scope of scopes) apiCache.invalidateByScope(scope);
    })
    .catch(() => {
      // apiCache may not be loaded yet during initial connection
    });
}

/**
 * Run a frame's side effects. Returns true when the frame is fully handled and must not
 * become a notification.
 */
export function routeFrame(frame: Frame): boolean {
  const { type } = frame;
  if (typeof type === 'string' && type.startsWith('support_access_')) {
    // Support-access grants (#1122): no MediaFile identity, so no quarantine filter.
    import('$lib/supportAccess/wsHandlers')
      .then(({ handleSupportAccessMessage }) =>
        handleSupportAccessMessage(frame as Parameters<typeof handleSupportAccessMessage>[0])
      )
      .catch(() => {});
    return true;
  }
  if (type === 'cache_invalidate') {
    // Push-based cache invalidation: drop the named scope, then tell listening components.
    invalidate([frame.data?.scope || 'all']);
    dispatch('cache-invalidated', frame.data);
    return true;
  }
  const route: FrameRoute | undefined = typeof type === 'string' ? FRAME_ROUTES[type] : undefined;
  if (!route) return false;
  if (route.event) dispatch(route.event, frame.data);
  if (route.scopes) invalidate(route.scopes);
  return route.consumed === true;
}
