import { render } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import AuditLogTable from './AuditLogTable.svelte';

const row = (id: string | undefined, event_type: string) =>
  ({
    id,
    timestamp: '2026-11-01T00:00:00Z',
    event_type,
    username: 'u',
    outcome: 'success',
    source_ip: '1.1.1.1',
    details: {},
  }) as never;

describe('AuditLogTable keys', () => {
  it('renders every row and raises no each_key_duplicate when rows carry ids', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { container } = render(AuditLogTable, {
      logs: [row('a', 'login'), row('b', 'logout')],
      total: 2,
      loading: false,
    });
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
    expect(err).not.toHaveBeenCalled();
    err.mockRestore();
  });

  it('fails loudly for rows without ids (the pre-fix backend shape)', () => {
    expect(() =>
      render(AuditLogTable, {
        logs: [row(undefined, 'login'), row(undefined, 'logout')],
        total: 2,
        loading: false,
      })
    ).toThrow();
  });
});
