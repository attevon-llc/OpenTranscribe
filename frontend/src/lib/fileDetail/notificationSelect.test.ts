import { describe, it, expect } from 'vitest';
import type { Notification } from '$stores/websocket';
import { isForFile, latestNotificationForFile, notificationSignature } from './notificationSelect';

function n(
  id: string,
  fileId: unknown,
  at: string,
  extra: Partial<Notification> = {}
): Notification {
  return {
    id,
    type: 'transcription_status',
    title: '',
    message: '',
    timestamp: new Date(at),
    read: false,
    data: fileId === undefined ? undefined : ({ file_id: fileId } as Notification['data']),
    ...extra,
  } as Notification;
}

describe('latestNotificationForFile', () => {
  it('returns the newest notification for this file only', () => {
    const list = [
      n('old', 'f1', '2026-01-01T00:00:00Z'),
      n('other', 'f2', '2026-01-03T00:00:00Z'),
      n('new', 'f1', '2026-01-02T00:00:00Z'),
    ];
    expect(latestNotificationForFile(list, 'f1')?.id).toBe('new');
  });

  it('returns null when nothing is for this file', () => {
    expect(latestNotificationForFile([n('x', 'f2', '2026-01-01T00:00:00Z')], 'f1')).toBeNull();
    expect(latestNotificationForFile([], 'f1')).toBeNull();
  });
});

describe('notificationSignature', () => {
  it('changes when an in-place progressive update moves, not only the id', () => {
    const base = n('p', 'f1', '2026-01-01T00:00:00Z', {
      status: 'processing',
      progress: { current: 1, total: 4, percentage: 25 },
      currentStep: 'asr',
    });
    const moved = { ...base, progress: { current: 2, total: 4, percentage: 50 } };
    expect(notificationSignature(base)).not.toBe(notificationSignature(moved));
    expect(notificationSignature(base)).toBe(notificationSignature({ ...base }));
  });
});

describe('isForFile', () => {
  it('matches as strings and never matches an unset id', () => {
    expect(isForFile(n('a', 7, '2026-01-01T00:00:00Z'), '7')).toBe(true);
    expect(isForFile(n('a', 'f2', '2026-01-01T00:00:00Z'), 'f1')).toBe(false);
    expect(isForFile(n('a', undefined, '2026-01-01T00:00:00Z'), 'undefined')).toBe(false);
  });
});
