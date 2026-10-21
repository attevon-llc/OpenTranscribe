import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cachedThumbnail, clearThumbnailCache } from './thumbnailCache';
import { setActiveSupportGrant } from '$lib/supportAccess/headers';

const GRANT = '11111111-2222-3333-4444-555555555555';

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, blob: async () => new Blob(['x']) });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal(
    'URL',
    Object.assign(URL, { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} })
  );
});

afterEach(() => {
  setActiveSupportGrant(null);
  clearThumbnailCache();
  vi.unstubAllGlobals();
});

async function load(uuid: string, url: string) {
  cachedThumbnail(document.createElement('img'), { uuid, url });
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
}

describe('thumbnail fetch and the support-access header', () => {
  it('sends the grant on the same-origin API fallback while a session is active', async () => {
    setActiveSupportGrant(GRANT);
    await load('t1', '/api/files/t1/thumbnail');
    expect(fetchMock).toHaveBeenCalledWith('/api/files/t1/thumbnail', {
      headers: { 'X-Support-Access-Grant': GRANT },
    });
  });

  it('never sends it to a presigned object-storage URL', async () => {
    setActiveSupportGrant(GRANT);
    await load('t2', 'https://minio.example/bucket/t2.jpg?X-Amz-Signature=abc');
    expect(fetchMock).toHaveBeenCalledWith(
      'https://minio.example/bucket/t2.jpg?X-Amz-Signature=abc',
      {
        headers: {},
      }
    );
  });

  it('sends nothing without a session', async () => {
    await load('t3', '/api/files/t3/thumbnail');
    expect(fetchMock).toHaveBeenCalledWith('/api/files/t3/thumbnail', { headers: {} });
  });
});
