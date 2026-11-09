import { describe, it, expect, beforeEach } from 'vitest';
import { discardPersistedUploads, loadPersistedUploads, persistUploads } from './persistence';
import type { UploadItem } from './types';

const item = (over: Partial<UploadItem>): UploadItem => ({
  id: 'x',
  type: 'file',
  source: new Blob(['a']),
  name: 'a.mp3',
  status: 'queued',
  progress: 40,
  retryCount: 0,
  ...over,
});

describe('upload persistence', () => {
  beforeEach(() => localStorage.clear());

  it('drops file sources but keeps url sources, and restores only retryable items as queued', () => {
    persistUploads(
      new Map([
        ['f', item({ id: 'f', status: 'failed', error: 'e' })],
        ['u', item({ id: 'u', type: 'url', source: 'https://x.test/v', status: 'failed' })],
        ['d', item({ id: 'd', status: 'completed' })],
      ])
    );
    const { entries, queueIds } = loadPersistedUploads();
    expect(entries.map(([id]) => id)).toEqual(['f', 'u']);
    expect(entries[0][1]).toMatchObject({ status: 'queued', progress: 0, source: null });
    expect(entries[0][1].error).toBeUndefined();
    expect(queueIds).toEqual(['u']);
  });

  it('tolerates corrupt storage and clears', () => {
    localStorage.setItem('upload_queue', '{not json');
    expect(loadPersistedUploads()).toEqual({ entries: [], queueIds: [] });
    localStorage.setItem('upload_queue', '[]');
    discardPersistedUploads();
    expect(localStorage.getItem('upload_queue')).toBeNull();
  });
});
