/**
 * `createDownloadStreamManager` — extracted from `TranscriptDisplay.svelte` (issue #748 J5).
 *
 * The SSE-stream branch (`openDownloadStream`) is not covered here: jsdom has no
 * `EventSource` implementation and none was stubbed for the pre-extraction code either —
 * this is a pre-existing coverage gap, not a regression introduced by the extraction.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { downloadStore } from '$stores/downloads';

vi.mock('$lib/axios', () => ({
  default: { post: vi.fn() },
}));

import axiosInstance from '$lib/axios';
import { createDownloadStreamManager } from './downloadStream';

describe('createDownloadStreamManager', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    downloadStore.subscribe(() => {})(); // no-op touch to keep the store defined
  });

  it('throws rather than silently no-op-ing when the file has no uuid', async () => {
    const manager = createDownloadStreamManager();
    await expect(manager.downloadMedia(null, 'audio_mp3')).rejects.toThrow();
    expect(axiosInstance.post).not.toHaveBeenCalled();
  });

  it('triggers an immediate download when the server returns a ready presigned URL', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({
      data: { status: 'ready', url: 'https://example.com/f.mp3', filename: 'f.mp3' },
    } as never);

    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    const manager = createDownloadStreamManager();
    await manager.downloadMedia({ uuid: 'file-1', filename: 'f.mp3' }, 'audio_mp3');

    expect(axiosInstance.post).toHaveBeenCalledWith(
      '/files/file-1/prepare-download',
      null,
      expect.objectContaining({ params: { mode: 'audio_mp3' } })
    );
    expect(clickSpy).toHaveBeenCalled();
    expect(get(downloadStore)['file-1'].status).toBe('completed');

    clickSpy.mockRestore();
  });

  it('marks the download errored when the prepare call rejects', async () => {
    vi.mocked(axiosInstance.post).mockRejectedValue(new Error('boom'));

    const manager = createDownloadStreamManager();
    await manager.downloadMedia({ uuid: 'file-2', filename: 'f.mp3' }, 'audio_mp3');

    expect(get(downloadStore)['file-2'].status).toBe('error');
  });

  it('refuses to start a second download for a file already in flight', async () => {
    vi.mocked(axiosInstance.post).mockImplementation(() => new Promise(() => {})); // never resolves

    const manager = createDownloadStreamManager();
    void manager.downloadMedia({ uuid: 'file-3', filename: 'f.mp3' }, 'audio_mp3');
    // Let the first call's synchronous startDownload() run before the second call races it.
    await Promise.resolve();

    await manager.downloadMedia({ uuid: 'file-3', filename: 'f.mp3' }, 'audio_mp3');

    expect(axiosInstance.post).toHaveBeenCalledTimes(1);
  });

  it('cleanup() is idempotent, and the manager keeps working after it runs', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({
      data: { status: 'ready', url: 'https://example.com/f.mp3', filename: 'f.mp3' },
    } as never);
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    const manager = createDownloadStreamManager();
    manager.cleanup();
    // A second call must not throw trying to re-close/re-clear already-empty maps.
    expect(() => manager.cleanup()).not.toThrow();

    // cleanup() must not leave the manager itself unusable — a real download still works.
    await manager.downloadMedia({ uuid: 'file-4', filename: 'f.mp3' }, 'audio_mp3');
    expect(get(downloadStore)['file-4'].status).toBe('completed');

    clickSpy.mockRestore();
  });
});
