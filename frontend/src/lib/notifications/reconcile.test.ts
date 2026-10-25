/**
 * Missed-frame fallback.
 *
 * DEFECTS THESE CATCH: a card that lost a frame waiting forever (nothing asks the server);
 * the panel hammering the endpoint on every re-render; and a file that is gone (deleted,
 * taken down) leaving its card stuck "Finishing up" instead of saying it cannot confirm.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const get = vi.fn();
vi.mock('$lib/axios', () => ({ default: { get: (...a: unknown[]) => get(...a) } }));

import { fetchPostProcessing, reconcileJobs, resetReconcileThrottle } from './reconcile';

beforeEach(() => {
  get.mockReset();
  resetReconcileThrottle();
});

describe('fetchPostProcessing', () => {
  it('reads the file endpoint and returns the snapshot', async () => {
    get.mockResolvedValue({ data: { file_id: 'f1', run_at: 5, tasks: {} } });
    await expect(fetchPostProcessing('f1')).resolves.toEqual({
      file_id: 'f1',
      run_at: 5,
      tasks: {},
    });
    expect(get).toHaveBeenCalledWith('/files/f1/post-processing');
  });

  it('a file the caller can no longer read answers "no run known"', async () => {
    get.mockRejectedValue(Object.assign(new Error('gone'), { response: { status: 404 } }));
    await expect(fetchPostProcessing('f1')).resolves.toEqual({
      file_id: 'f1',
      run_at: null,
      tasks: {},
    });
  });

  it('a network failure answers nothing, so the card keeps what it has', async () => {
    get.mockRejectedValue(new Error('offline'));
    await expect(fetchPostProcessing('f1')).resolves.toBeNull();
  });
});

describe('reconcileJobs', () => {
  it('asks only for jobs that need it, once per file per throttle window', async () => {
    const fetcher = vi.fn().mockResolvedValue({ file_id: 'a', run_at: 1, tasks: {} });
    const applied: unknown[] = [];
    const jobs = [
      { fileId: 'a', reconcile: true },
      { fileId: 'b', reconcile: false },
    ];

    reconcileJobs(jobs, 1_000, (s) => applied.push(s), fetcher);
    reconcileJobs(jobs, 30_000, (s) => applied.push(s), fetcher);
    await vi.waitFor(() => expect(applied).toHaveLength(1));
    expect(fetcher.mock.calls).toEqual([['a']]);

    reconcileJobs(jobs, 70_000, (s) => applied.push(s), fetcher);
    await vi.waitFor(() => expect(applied).toHaveLength(2));
    expect(fetcher.mock.calls).toEqual([['a'], ['a']]);
  });
});
