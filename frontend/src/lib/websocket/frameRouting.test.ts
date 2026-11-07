/**
 * Pins the realtime frame side effects: the window event each frame type raises (a
 * contract with settings panels and pages that listen by name), the `apiCache` scopes
 * it drops, and whether it is consumed or continues to a notification. A frame that is
 * consumed by mistake never reaches the bell; one that is not consumed toasts on every
 * GPU-stats tick.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiCache } from '$lib/apiCache';
import { routeFrame } from './frameRouting';

vi.mock('$lib/supportAccess/wsHandlers', () => ({ handleSupportAccessMessage: vi.fn() }));

const SHARE = ['collections', 'files', 'shares', 'shared_collections'];

// type, event (or null), scopes, consumed
const CASES: Array<[string, string | null, string[], boolean]> = [
  ['connection_established', null, [], true],
  ['echo', null, [], true],
  ['gpu_stats_update', 'gpu-stats-updated', [], true],
  ['local_model_download', 'local-model-download', [], true],
  ['watch_source_scan', 'watch-source-scan', [], true],
  ['reindex_progress', 'reindex-progress', [], false],
  ['reindex_complete', 'reindex-complete', [], false],
  ['reindex_stopped', 'reindex-stopped', [], false],
  ['migration_progress', 'migration-progress', [], false],
  ['migration_complete', 'migration-complete', [], false],
  ['migration_finalized', 'migration-finalized', [], false],
  ['attribute_migration_progress', 'attribute-migration-progress', [], false],
  ['attribute_migration_complete', 'attribute-migration-complete', [], false],
  ['data_integrity_progress', 'data-integrity-progress', [], false],
  ['data_integrity_complete', 'data-integrity-complete', [], false],
  ['embedding_consistency_progress', 'embedding-consistency-progress', [], false],
  ['embedding_consistency_complete', 'embedding-consistency-complete', [], false],
  ['speaker_updated', 'speaker-updated', [], true],
  ['speaker_processing_complete', 'speaker-processing-complete', [], true],
  ['speaker_rename_propagation', 'speaker-rename-propagation', ['files', 'speakers'], true],
  ['clustering_progress', 'clustering-progress', [], false],
  ['clustering_complete', 'clustering-complete', [], false],
  ['clustering_file_complete', 'clustering-file-complete', [], true],
  ['collection_shared', 'collection-shared', SHARE, false],
  ['collection_share_revoked', 'share-revoked', SHARE, false],
  [
    'collection_share_updated',
    'share-updated',
    ['collections', 'shares', 'shared_collections'],
    false,
  ],
  ['collection_access_changed', 'collection-access-changed', SHARE, false],
  ['group_member_added', 'group-member-added', SHARE, false],
  ['group_member_removed', 'group-member-removed', SHARE, false],
  ['file_takedown', null, ['files'], false],
  ['file_takedown_released', null, ['files'], false],
  ['file_trashed', 'file-trashed', ['files'], true],
  ['file_restored', 'file-restored', ['files'], true],
  ['auto_label_status', 'auto-label-status', [], false],
  // Plain notification types: no side effect, not consumed.
  ['transcription_status', null, [], false],
  ['file_created', null, [], false],
];

let events: Array<{ type: string; detail: unknown }>;
const record = (e: Event) => events.push({ type: e.type, detail: (e as CustomEvent).detail });
const origDispatch = window.dispatchEvent.bind(window);

beforeEach(() => {
  events = [];
  vi.spyOn(window, 'dispatchEvent').mockImplementation((e: Event) => {
    record(e);
    return origDispatch(e);
  });
  vi.spyOn(apiCache, 'invalidateByScope');
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function flushImports() {
  await vi.dynamicImportSettled();
}

describe('routeFrame', () => {
  it.each(CASES)(
    '%s -> event %s, scopes %j, consumed %s',
    async (type, event, scopes, consumed) => {
      const detail = { file_id: 'f1' };
      expect(routeFrame({ type, data: detail })).toBe(consumed);
      await flushImports();
      expect(events).toEqual(event ? [{ type: event, detail }] : []);
      expect(vi.mocked(apiCache.invalidateByScope).mock.calls.map((c) => c[0])).toEqual(scopes);
    }
  );

  it('cache_invalidate drops the named scope, defaulting to all, and is consumed', async () => {
    expect(routeFrame({ type: 'cache_invalidate', data: { scope: 'tags' } })).toBe(true);
    expect(routeFrame({ type: 'cache_invalidate', data: {} })).toBe(true);
    await flushImports();
    expect(vi.mocked(apiCache.invalidateByScope).mock.calls.map((c) => c[0])).toEqual([
      'tags',
      'all',
    ]);
    expect(events.map((e) => e.type)).toEqual(['cache-invalidated', 'cache-invalidated']);
  });

  it('hands support_access_* frames to the support-access handler and consumes them', async () => {
    const { handleSupportAccessMessage } = await import('$lib/supportAccess/wsHandlers');
    const frame = { type: 'support_access_started', data: { grant: 'g' } };
    expect(routeFrame(frame)).toBe(true);
    await flushImports();
    expect(handleSupportAccessMessage).toHaveBeenCalledWith(frame);
    expect(events).toEqual([]);
  });

  it('does not treat inherited object keys or non-string types as routes', () => {
    expect(routeFrame({ type: 'toString', data: {} })).toBe(false);
    expect(routeFrame({ type: 42, data: {} })).toBe(false);
    expect(events).toEqual([]);
  });
});
