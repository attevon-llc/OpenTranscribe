import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  clearSectionData,
  hasSectionPrefetch,
  invalidateSectionData,
  loadSectionData,
  peekSectionData,
  prefetchSectionData,
  sameSectionData,
  setSectionData,
} from './sectionData';

const getMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/axios', () => ({ default: { get: getMock } }));
const pipelineMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/pipeline', () => ({ getPipelineView: pipelineMock }));
const authMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/authConfig', () => ({ AuthConfigApi: { getAllConfigs: authMock } }));

beforeEach(() => {
  clearSectionData();
  getMock.mockReset().mockResolvedValue({ data: { ok: 1 } });
  pipelineMock.mockReset().mockResolvedValue({ steps: [] });
  authMock.mockReset().mockResolvedValue({ local: [] });
});

describe('section data cache', () => {
  it('is a cold miss until something is stored, then returns it', async () => {
    expect(peekSectionData('k')).toBeUndefined();
    await loadSectionData('k', async () => 'v');
    expect(peekSectionData('k')).toBe('v');
  });

  it('does not show a value older than the max age', () => {
    vi.useFakeTimers();
    setSectionData('k', 'v');
    vi.advanceTimersByTime(60_000);
    expect(peekSectionData('k', 30_000)).toBeUndefined();
    vi.useRealTimers();
  });

  it('joins concurrent loads into one request', async () => {
    const fetcher = vi.fn().mockResolvedValue('v');
    await Promise.all([loadSectionData('k', fetcher), loadSectionData('k', fetcher)]);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('drops a response that was in flight when the key was invalidated', async () => {
    let resolve!: (v: string) => void;
    const slow = new Promise<string>((r) => (resolve = r));
    const load = loadSectionData('k', () => slow);
    invalidateSectionData('k');
    resolve('stale');
    await load;
    expect(peekSectionData('k')).toBeUndefined();
  });

  it('clear forgets everything, including a response still on its way (logout)', async () => {
    setSectionData('a', 1);
    let resolve!: (v: number) => void;
    const load = loadSectionData('b', () => new Promise<number>((r) => (resolve = r)));
    clearSectionData();
    resolve(2);
    await load;
    expect(peekSectionData('a')).toBeUndefined();
    expect(peekSectionData('b')).toBeUndefined();
  });

  it('does not store a failed load', async () => {
    await expect(loadSectionData('k', () => Promise.reject(new Error('x')))).rejects.toThrow();
    expect(peekSectionData('k')).toBeUndefined();
    // and a retry is allowed (the failed request is no longer "in flight")
    await loadSectionData('k', async () => 'v');
    expect(peekSectionData('k')).toBe('v');
  });

  it('compares payloads structurally', () => {
    expect(sameSectionData({ a: [1] }, { a: [1] })).toBe(true);
    expect(sameSectionData({ a: [1] }, { a: [2] })).toBe(false);
  });
});

describe('background data prefetch is role-aware and cheap', () => {
  it('fetches the pipeline view for any user', async () => {
    prefetchSectionData('processing-pipeline', 'user');
    await vi.waitFor(() => expect(peekSectionData('settings:pipeline')).toEqual({ steps: [] }));
    expect(pipelineMock).toHaveBeenCalledTimes(1);
  });

  it('never calls an admin read for a plain user, nor the auth config for an admin', async () => {
    prefetchSectionData('data-management', 'user');
    prefetchSectionData('authentication', 'admin');
    prefetchSectionData('authentication', 'user');
    await new Promise((r) => setTimeout(r, 0));
    expect(getMock).not.toHaveBeenCalled();
    expect(authMock).not.toHaveBeenCalled();
    expect(peekSectionData('settings:retention')).toBeUndefined();
    expect(peekSectionData('settings:auth-config')).toBeUndefined();
  });

  it('calls the retention read for an admin and the auth config for a super admin', async () => {
    prefetchSectionData('data-management', 'admin');
    prefetchSectionData('authentication', 'super_admin');
    await vi.waitFor(() => expect(peekSectionData('settings:retention')).toEqual({ ok: 1 }));
    await vi.waitFor(() => expect(peekSectionData('settings:auth-config')).toEqual({ local: [] }));
    expect(getMock).toHaveBeenCalledWith('/admin/settings/retention-config');
  });

  it('registers no warm-up for the heavy sections', () => {
    for (const heavy of ['system-statistics', 'admin-task-health', 'search-indexing'] as const) {
      expect(hasSectionPrefetch(heavy, 'super_admin')).toBe(false);
    }
  });

  it('does not refetch an entry that is still fresh', async () => {
    prefetchSectionData('processing-pipeline', 'user');
    await vi.waitFor(() => expect(peekSectionData('settings:pipeline')).toEqual({ steps: [] }));
    pipelineMock.mockResolvedValue({ steps: ['changed'] });
    prefetchSectionData('processing-pipeline', 'user');
    await new Promise((r) => setTimeout(r, 0));
    // Still the first answer: the fresh entry was left alone, not refetched over.
    expect(peekSectionData('settings:pipeline')).toEqual({ steps: [] });
  });
});
