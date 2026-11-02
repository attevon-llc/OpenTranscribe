import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

import { createSegmentPager, redactParams } from './segmentPaging';
import type { SegmentBearingFile } from './segmentSync';

function seg(i: number) {
  return { uuid: `s${i}`, start_time: i, end_time: i + 1, text: `t${i}` };
}

function page(from: number, to: number, total: number) {
  const segs = [];
  for (let i = from; i < to; i++) segs.push(seg(i));
  return { data: { transcript_segments: segs, grouped_segments: [], total_segments: total } };
}

function harness(loaded: number, total: number, opts: { showOriginal?: boolean } = {}) {
  const state = {
    file: page(0, loaded, total).data as SegmentBearingFile,
    total,
    loading: false,
    files: [] as SegmentBearingFile[],
  };
  const deps = { sync: vi.fn(), schedule: vi.fn(), notifyError: vi.fn() };
  const pager = createSegmentPager({
    getFileId: () => 'f1',
    getFile: () => state.file,
    setFile: (f) => {
      state.file = f;
      state.files.push(f);
    },
    getShowOriginal: () => opts.showOriginal ?? false,
    getSegmentLimit: () => 2,
    getTotalSegments: () => state.total,
    setTotalSegments: (t) => (state.total = t),
    isLoadingMore: () => state.loading,
    setLoadingMore: (l) => (state.loading = l),
    hasMoreSegments: () => state.total > (state.file.transcript_segments?.length || 0),
    syncTranscriptStore: deps.sync,
    schedule: deps.schedule,
    notifyError: deps.notifyError,
  });
  return { state, pager, deps };
}

function uuids(file: SegmentBearingFile) {
  return file.transcript_segments?.map((s) => s.uuid);
}

beforeEach(() => vi.clearAllMocks());

describe('redactParams', () => {
  it('asks for the original only when the reader has it on', () => {
    expect(redactParams(true)).toEqual({ redact: false });
    expect(redactParams(false)).toEqual({});
  });
});

describe('loadMore', () => {
  it('fetches the next page from the loaded count, honouring show-original', async () => {
    mockAxios.get.mockResolvedValue(page(2, 4, 6));
    const { state, pager, deps } = harness(2, 6, { showOriginal: true });

    await pager.loadMore();

    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1/segments', {
      params: { segment_limit: 2, segment_offset: 2, redact: false },
    });
    expect(uuids(state.file)).toEqual(['s0', 's1', 's2', 's3']);
    expect(state.loading).toBe(false);
    expect(deps.sync).toHaveBeenCalledTimes(1);
  });

  it('does nothing when everything is loaded', async () => {
    const { state, pager } = harness(2, 2);
    await pager.loadMore();
    expect(mockAxios.get).not.toHaveBeenCalled();
    expect(state.files).toEqual([]);
    expect(state.loading).toBe(false);
  });

  it('tells the reader when a page fails, and clears the loading flag', async () => {
    mockAxios.get.mockRejectedValue(new Error('down'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { state, pager, deps } = harness(2, 6);

    await pager.loadMore();

    expect(deps.notifyError).toHaveBeenCalledWith('fileDetail.failedToLoadMoreSegments');
    expect(state.loading).toBe(false);
  });
});

describe('loadUpTo', () => {
  it('pages in a loop until the target index is loaded, then schedules the scroll', async () => {
    mockAxios.get.mockResolvedValueOnce(page(2, 5, 3000)).mockResolvedValueOnce(page(5, 60, 3000));
    const { state, pager, deps } = harness(2, 3000);

    await pager.loadUpTo(5, 's5');

    expect(mockAxios.get.mock.calls.map(([, o]) => o.params.segment_offset)).toEqual([2, 5]);
    expect(mockAxios.get.mock.calls[0][1].params.segment_limit).toBe(5 - 2 + 50 + 1);
    expect(state.file.transcript_segments?.length).toBe(60);
    expect(deps.sync).toHaveBeenCalledTimes(1);
    expect(deps.schedule).toHaveBeenCalledWith(expect.any(Function), 300);
  });

  it('caps a single request at the server page maximum', async () => {
    mockAxios.get.mockResolvedValue(page(2, 2002, 2002));
    const { pager } = harness(2, 2002);
    await pager.loadUpTo(5000, 'x');
    expect(mockAxios.get.mock.calls[0][1].params.segment_limit).toBe(2000);
  });

  it('stops when the server returns no progress', async () => {
    mockAxios.get.mockResolvedValue(page(0, 0, 10));
    const { state, pager } = harness(2, 10);
    await pager.loadUpTo(8, 's8');
    expect(mockAxios.get).toHaveBeenCalledTimes(1);
    expect(state.file.transcript_segments?.length).toBe(2);
    expect(state.loading).toBe(false);
  });

  it('only scrolls when the target is already loaded', async () => {
    const { pager, deps } = harness(4, 10);
    await pager.loadUpTo(1, 's1');
    expect(mockAxios.get).not.toHaveBeenCalled();
    expect(deps.schedule).not.toHaveBeenCalled();
  });
});

describe('refetchAfterMerge', () => {
  it('empties the transcript, then replays exactly as many segments as were loaded', async () => {
    mockAxios.get.mockResolvedValueOnce(page(0, 2, 10)).mockResolvedValueOnce(page(2, 3, 10));
    const { state, pager, deps } = harness(3, 10);

    await pager.refetchAfterMerge();

    expect(state.files[0].transcript_segments).toEqual([]);
    expect(mockAxios.get.mock.calls.map(([, o]) => o.params)).toEqual([
      { segment_limit: 2, segment_offset: 0 },
      { segment_limit: 1, segment_offset: 2 },
    ]);
    expect(uuids(state.file)).toEqual(['s0', 's1', 's2']);
    expect(deps.sync).toHaveBeenCalledTimes(1);
  });
});
