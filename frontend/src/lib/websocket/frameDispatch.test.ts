import { beforeEach, describe, expect, it, vi } from 'vitest';

const { downloadStore, axiosGet, routeFrame } = vi.hoisted(() => ({
  downloadStore: { updateStatus: vi.fn() },
  axiosGet: vi.fn(),
  routeFrame: vi.fn(() => false),
}));

vi.mock('$stores/downloads', () => ({ downloadStore }));
vi.mock('$lib/axios', () => ({ default: { get: axiosGet } }));
vi.mock('$lib/websocket/frameRouting', () => ({ routeFrame }));
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import { dispatchMessage, recoverActiveProgress } from './frameDispatch';

const spies = {
  foldProgressFrame: vi.fn(),
  applyPostProcessing: vi.fn(),
  addSilentFrame: vi.fn(),
  addRegularFrame: vi.fn(),
  upsertRecoveredTask: vi.fn(),
};
const notifications = spies as unknown as Parameters<typeof dispatchMessage>[1];

beforeEach(() => {
  vi.clearAllMocks();
  routeFrame.mockReturnValue(false);
});

describe('dispatchMessage', () => {
  it('mirrors a download_progress frame into the download store and files nothing else', () => {
    dispatchMessage(
      JSON.stringify({
        type: 'download_progress',
        data: { file_id: 'f1', status: 'downloading', progress: 40 },
      }),
      notifications
    );
    expect(downloadStore.updateStatus).toHaveBeenCalledWith('f1', 'downloading', 40, undefined);
    expect(routeFrame).not.toHaveBeenCalled();
  });

  it('stops after the router consumes a frame', () => {
    routeFrame.mockReturnValue(true);
    dispatchMessage(JSON.stringify({ type: 'speaker_updated', data: {} }), notifications);
    expect(routeFrame).toHaveBeenCalledTimes(1);
    expect(spies.addRegularFrame).not.toHaveBeenCalled();
  });

  it('files an ordinary frame as a regular notification', () => {
    dispatchMessage(
      JSON.stringify({ type: 'file_deleted', data: { message: 'x' } }),
      notifications
    );
    expect(spies.addRegularFrame).toHaveBeenCalledWith('file_deleted', {
      message: 'x',
    });
  });

  it('logs a malformed frame instead of throwing', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => dispatchMessage('{not json', notifications)).not.toThrow();
    expect(error).toHaveBeenCalled();
    error.mockRestore();
  });
});

describe('recoverActiveProgress', () => {
  it('does nothing when no task is active', async () => {
    axiosGet.mockResolvedValue({ data: [] });
    await recoverActiveProgress(notifications);
    expect(spies.upsertRecoveredTask).not.toHaveBeenCalled();
  });

  it('swallows a failed fetch: recovery is best-effort', async () => {
    axiosGet.mockRejectedValue(new Error('offline'));
    await expect(recoverActiveProgress(notifications)).resolves.toBeUndefined();
  });
});
