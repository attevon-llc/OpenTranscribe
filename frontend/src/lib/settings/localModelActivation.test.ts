import { describe, it, expect, vi } from 'vitest';
import { switchConfirmed, activateLocalModel } from './localModelActivation';

const restart = (over = {}) => ({
  status: 'restart_signaled',
  model: 'tiny',
  workers: ['gpu-transcription@a'],
  active_tasks: 2,
  restart_requested_at: '2026-10-15T10:00:00+00:00',
  message: 'x',
  ...over,
});

const loaded = (worker: string, model: string, ready_at: string) => ({ worker, model, ready_at });

describe('switchConfirmed', () => {
  it('is true once every restarted worker reports the new model after the restart', () => {
    expect(
      switchConfirmed(restart(), [
        loaded('gpu-transcription@a', 'tiny', '2026-10-15T10:01:00+00:00'),
      ])
    ).toBe(true);
  });

  it('is false while the worker still reports the old model', () => {
    expect(
      switchConfirmed(restart(), [
        loaded('gpu-transcription@a', 'large-v3', '2026-10-15T10:01:00+00:00'),
      ])
    ).toBe(false);
  });

  it('is false when the report predates the restart request', () => {
    expect(
      switchConfirmed(restart(), [
        loaded('gpu-transcription@a', 'tiny', '2026-10-15T09:59:00+00:00'),
      ])
    ).toBe(false);
  });

  it('needs every restarted worker, and ignores workers it did not restart', () => {
    const two = restart({ workers: ['w1', 'w2'] });
    const ready = '2026-10-15T10:01:00+00:00';
    expect(switchConfirmed(two, [loaded('w1', 'tiny', ready)])).toBe(false);
    expect(
      switchConfirmed(two, [
        loaded('w1', 'tiny', ready),
        loaded('w2', 'tiny', ready),
        loaded('other', 'old', '2026-10-15T08:00:00+00:00'),
      ])
    ).toBe(true);
  });

  it('is false for an empty report or an unparseable time', () => {
    expect(switchConfirmed(restart(), [])).toBe(false);
    expect(switchConfirmed(restart(), [loaded('gpu-transcription@a', 'tiny', 'garbage')])).toBe(
      false
    );
  });
});

describe('activateLocalModel', () => {
  const ready = loaded('gpu-transcription@a', 'tiny', '2026-10-15T10:01:00+00:00');

  it('sets, then restarts, then polls until the worker confirms', async () => {
    const order: string[] = [];
    const api = {
      setLocalModel: vi.fn(async () => void order.push('set')),
      restartGpuWorker: vi.fn(async () => (order.push('restart'), restart())),
      getLoadedModels: vi.fn().mockResolvedValueOnce([]).mockResolvedValueOnce([ready]),
    };
    const onRestarted = vi.fn();
    const result = await activateLocalModel('tiny', {
      api,
      onRestarted,
      sleep: async () => {},
      intervalMs: 1,
      timeoutMs: 1000,
    });
    expect(order).toEqual(['set', 'restart']);
    expect(onRestarted).toHaveBeenCalledWith(2);
    expect(api.getLoadedModels).toHaveBeenCalledTimes(2);
    expect(result).toBe('confirmed');
  });

  it('does not restart when setting the model fails', async () => {
    const api = {
      setLocalModel: vi.fn().mockRejectedValue(new Error('409')),
      restartGpuWorker: vi.fn(),
      getLoadedModels: vi.fn(),
    };
    await expect(
      activateLocalModel('tiny', { api, onRestarted: vi.fn(), sleep: async () => {} })
    ).rejects.toThrow('409');
    expect(api.restartGpuWorker).not.toHaveBeenCalled();
  });

  it('gives up with "pending" when the worker never reports in time', async () => {
    let now = 0;
    const api = {
      setLocalModel: vi.fn().mockResolvedValue(undefined),
      restartGpuWorker: vi.fn().mockResolvedValue(restart()),
      getLoadedModels: vi.fn().mockResolvedValue([]),
    };
    const result = await activateLocalModel('tiny', {
      api,
      onRestarted: vi.fn(),
      sleep: async () => void (now += 500),
      now: () => now,
      intervalMs: 500,
      timeoutMs: 2000,
    });
    expect(result).toBe('pending');
  });

  it('stops polling when aborted', async () => {
    const controller = new AbortController();
    const api = {
      setLocalModel: vi.fn().mockResolvedValue(undefined),
      restartGpuWorker: vi.fn().mockResolvedValue(restart()),
      getLoadedModels: vi.fn().mockImplementation(async () => {
        controller.abort();
        return [];
      }),
    };
    const result = await activateLocalModel('tiny', {
      api,
      onRestarted: vi.fn(),
      sleep: async () => {},
      signal: controller.signal,
    });
    expect(result).toBe('aborted');
  });
});
