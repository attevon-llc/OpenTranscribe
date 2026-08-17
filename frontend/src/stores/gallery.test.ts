/**
 * `galleryStore` drives the file gallery's selection, pagination, and filter
 * persistence. Tests focus on the logic most likely to desync silently: range
 * selection with a stale/missing anchor, page-append deduplication (the comment
 * on `appendFiles` calls out keyed `{#each}` errors from duplicate uuids across
 * pages during status-change reshuffles), and filter save/reset not sharing
 * array/object references with the caller.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { galleryStore } from './gallery';
import type { MediaFile } from '$lib/types/media';

function file(uuid: string): MediaFile {
  return { uuid, filename: `${uuid}.mp3` } as MediaFile;
}

beforeEach(() => {
  localStorage.clear();
  galleryStore.setFiles([]);
  galleryStore.clearSelection();
  galleryStore.resetFilters();
  galleryStore.resetPagination();
});

describe('setViewMode', () => {
  it('persists the choice to localStorage and updates state', () => {
    galleryStore.setViewMode('list');

    expect(get(galleryStore).viewMode).toBe('list');
    expect(localStorage.getItem('gallery-view-mode')).toBe('list');
  });
});

describe('toggleFileSelection', () => {
  it('adds then removes a file, tracking it as the last-selected anchor', () => {
    galleryStore.toggleFileSelection('a');
    expect(get(galleryStore).selectedFiles.has('a')).toBe(true);
    expect(get(galleryStore).lastSelectedId).toBe('a');

    galleryStore.toggleFileSelection('a');
    expect(get(galleryStore).selectedFiles.has('a')).toBe(false);
  });
});

describe('handleMultiSelect', () => {
  beforeEach(() => {
    galleryStore.setFiles([file('a'), file('b'), file('c'), file('d')]);
  });

  it('shift-click selects the inclusive range from the anchor', () => {
    galleryStore.handleMultiSelect('a', false, false); // sets the anchor
    galleryStore.handleMultiSelect('c', false, true); // shift-click

    const selected = get(galleryStore).selectedFiles;
    expect([...selected].sort()).toEqual(['a', 'b', 'c']);
  });

  it('falls back to a single toggle when shift-clicked with no anchor set', () => {
    galleryStore.handleMultiSelect('b', false, true);

    expect([...get(galleryStore).selectedFiles]).toEqual(['b']);
  });

  it('falls back to toggling just the target when the anchor id no longer exists in the file list', () => {
    galleryStore.handleMultiSelect('a', false, false); // selects 'a', sets it as anchor
    galleryStore.setFiles([file('b'), file('c')]); // 'a' is gone (e.g. deleted)

    galleryStore.handleMultiSelect('c', false, true);

    // The anchor can't be resolved to an index in the (now-different) file list, so the
    // range-select math is skipped in favor of toggling only the clicked file — it does
    // NOT clear the stale 'a' selection, which is a real, separate gap: a uuid selected
    // before its file left the loaded list stays counted (e.g. in selectedCount) forever.
    expect([...get(galleryStore).selectedFiles].sort()).toEqual(['a', 'c']);
  });

  it('derives isSelecting from whether anything ended up selected', () => {
    galleryStore.handleMultiSelect('a', true, false);
    expect(get(galleryStore).isSelecting).toBe(true);

    galleryStore.handleMultiSelect('a', true, false); // toggle back off
    expect(get(galleryStore).isSelecting).toBe(false);
  });
});

describe('selectAllFiles', () => {
  it('selects every file, then deselects all on a second call', () => {
    galleryStore.setFiles([file('a'), file('b')]);

    galleryStore.selectAllFiles();
    expect(get(galleryStore).selectedFiles.size).toBe(2);

    galleryStore.selectAllFiles();
    expect(get(galleryStore).selectedFiles.size).toBe(0);
  });
});

describe('appendFiles', () => {
  it('replaces the list on page 1', () => {
    galleryStore.appendFiles([file('a')], {
      page: 1,
      pageSize: 100,
      total: 1,
      totalPages: 1,
      hasMore: false,
    });
    galleryStore.appendFiles([file('b')], {
      page: 1,
      pageSize: 100,
      total: 1,
      totalPages: 1,
      hasMore: false,
    });

    expect(get(galleryStore).files.map((f) => f.uuid)).toEqual(['b']);
  });

  it('deduplicates by uuid across pages instead of allowing a duplicate keyed entry', () => {
    galleryStore.appendFiles([file('a'), file('b')], {
      page: 1,
      pageSize: 2,
      total: 3,
      totalPages: 2,
      hasMore: true,
    });
    // 'b' reshuffled onto page 2 (e.g. its status changed and sort order moved it)
    // alongside a genuinely new 'c'.
    galleryStore.appendFiles([file('b'), file('c')], {
      page: 2,
      pageSize: 2,
      total: 3,
      totalPages: 2,
      hasMore: false,
    });

    const uuids = get(galleryStore).files.map((f) => f.uuid);
    expect(uuids).toEqual(['a', 'b', 'c']);
  });

  it('tolerates a null/undefined files payload from a failed API response', () => {
    expect(() =>
      // @ts-expect-error deliberately simulating a malformed API response
      galleryStore.appendFiles(null, {
        page: 1,
        pageSize: 100,
        total: 0,
        totalPages: 0,
        hasMore: false,
      })
    ).not.toThrow();
    expect(get(galleryStore).files).toEqual([]);
  });
});

describe('resetPagination', () => {
  it('clears pagination metadata without touching the loaded files', () => {
    galleryStore.appendFiles([file('a')], {
      page: 1,
      pageSize: 100,
      total: 5,
      totalPages: 2,
      hasMore: true,
    });

    galleryStore.resetPagination();

    const state = get(galleryStore);
    expect(state.currentPage).toBe(0);
    expect(state.hasMoreFiles).toBe(false);
    expect(state.files).toHaveLength(1); // resetPagination is not "clear files"
  });
});

describe('saveFilters / resetFilters', () => {
  it('copies arrays/objects rather than holding the caller reference', () => {
    const tags = ['a', 'b'];
    const dateRange: { from: Date | null; to: Date | null } = {
      from: new Date('2026-01-01'),
      to: null,
    };
    galleryStore.saveFilters({
      searchQuery: 'q',
      selectedTags: tags,
      selectedSpeakers: [],
      selectedCollectionId: null,
      dateRange,
      durationRange: { min: null, max: null },
      fileSizeRange: { min: null, max: null },
      selectedFileTypes: [],
      selectedStatuses: [],
      ownershipFilter: 'all',
      sortBy: 'upload_time',
      sortOrder: 'desc',
    });

    tags.push('mutated-after-save');
    dateRange.from = null;

    const state = get(galleryStore);
    expect(state.filterSelectedTags).toEqual(['a', 'b']);
    expect(state.filterDateRange.from).toEqual(new Date('2026-01-01'));
  });

  it('resetFilters restores every filter field to its default', () => {
    galleryStore.saveFilters({
      searchQuery: 'q',
      selectedTags: ['a'],
      selectedSpeakers: ['s1'],
      selectedCollectionId: 'c1',
      dateRange: { from: new Date(), to: new Date() },
      durationRange: { min: 1, max: 2 },
      fileSizeRange: { min: 1, max: 2 },
      selectedFileTypes: ['mp3'],
      selectedStatuses: ['completed'],
      ownershipFilter: 'mine',
      sortBy: 'filename',
      sortOrder: 'asc',
    });

    galleryStore.resetFilters();

    const state = get(galleryStore);
    expect(state.filterSearchQuery).toBe('');
    expect(state.filterSelectedTags).toEqual([]);
    expect(state.filterOwnershipFilter).toBe('all');
    expect(state.filterSortOrder).toBe('desc');
  });
});

describe('action triggers', () => {
  it('skip the initial subscribe-time value and fire only on a later trigger', () => {
    const seen: number[] = [];
    const unsubscribe = galleryStore.onUploadTrigger((v) => seen.push(v));

    expect(seen).toEqual([]); // subscribing alone must not fire the callback
    galleryStore.triggerUpload();
    expect(seen).toEqual([1]);

    unsubscribe();
  });

  it('export trigger resets to empty after firing, so the same format can retrigger', () => {
    const seen: string[] = [];
    const unsubscribe = galleryStore.onExportTrigger((v) => seen.push(v));

    galleryStore.triggerExport('csv');
    galleryStore.triggerExport('csv');

    expect(seen).toEqual(['csv', 'csv']);
    unsubscribe();
  });
});
