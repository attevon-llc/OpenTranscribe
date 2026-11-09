/**
 * A `monitoring_alert` frame carries Prometheus alert names and annotation text (issue #791):
 * technical, English, and left-to-right. In a right-to-left locale the row must pin that text
 * to `dir="ltr"` or the alert name and its punctuation are reordered.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import NotificationRow from './NotificationRow.svelte';
import type { Notification } from '$stores/websocket';

function row(type: string): Notification {
  return {
    id: 'n1',
    type,
    title: 'Monitoring alert',
    message: 'AuditOutboxStuck: Governance audit events are not reaching the audit log',
    timestamp: new Date(),
    read: false,
    data: { status: 'failed' },
    dismissible: true,
  } as unknown as Notification;
}

describe('NotificationRow monitoring_alert', () => {
  it('pins the alert text to left-to-right', () => {
    const { container } = render(NotificationRow, {
      notification: row('monitoring_alert'),
      now: Date.now(),
    });
    expect(container.querySelector('.notification-message')?.getAttribute('dir')).toBe('ltr');
  });

  it('leaves other notifications direction-neutral', () => {
    const { container } = render(NotificationRow, {
      notification: row('backup_status'),
      now: Date.now(),
    });
    expect(container.querySelector('.notification-message')?.getAttribute('dir')).toBeNull();
  });
});
