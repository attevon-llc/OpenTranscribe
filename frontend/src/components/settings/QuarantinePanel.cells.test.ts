/** The queue's dates and cells: formatted in the interface language, never raw ISO or mid-word breaks. */
import { describe, it, expect, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('de'), () => {}) },
}));

import axiosInstance from '$lib/axios';
import QuarantinePanel from './QuarantinePanel.svelte';

const row = {
  uuid: 'f1',
  filename: 'a.wav',
  title: null,
  owner_uuid: 'o1',
  owner_display: 'owner-with-a-very-long-address@example-organisation.com',
  quarantined_at: '2026-11-03T10:00:00Z',
  legal_hold: false,
  is_quarantined: true,
  takedown_origin: 'organization',
  can_release: true,
  quarantine_reason: 'r',
  quarantined_by_display: 'Ada',
};
const css = readFileSync(resolve(__dirname, 'QuarantinePanel.svelte'), 'utf8').split('<style>')[1];

describe('QuarantinePanel cells', () => {
  const rows = async () => {
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: { files: [row], total: 1 } } as never);
    const { container } = render(QuarantinePanel, { scope: 'organization' });
    await waitFor(() => expect(container.querySelector('tbody tr')).not.toBeNull());
    return container;
  };
  const rule = (sel: string) =>
    css.match(new RegExp(`${sel.replace(/[.]/g, '\\$&')}\\s*(?:,[^{]*)?\\{([^}]*)\\}`))?.[1] ?? '';

  it('truncates the file name and people with an ellipsis and a title', async () => {
    const c = await rows();
    expect(c.querySelector('.file-cell a')?.getAttribute('title')).toBe('a.wav');
    expect(c.querySelector('.person')?.getAttribute('title')).toBe(row.owner_display);
    expect(rule('.person')).toMatch(/text-overflow:\s*ellipsis/);
    expect(rule('.file-cell')).toMatch(/min-width:\s*\d+px/);
  });

  it('stacks owner and actor in one cell: six columns, not eight', async () => {
    const c = await rows();
    expect(c.querySelectorAll('thead th').length).toBe(6);
    expect(c.querySelectorAll('tbody tr:first-child > td').length).toBe(6);
    expect(c.querySelectorAll('.people-cell .person').length).toBe(2);
  });

  it('never breaks inside a word: badges, headers and actions are nowrap, nothing forces breaks', () => {
    expect(rule('.badge')).toMatch(/white-space:\s*nowrap/);
    expect(css).toMatch(/th\s*\{[^}]*white-space:\s*nowrap/);
    expect(rule('.actions-cell')).toMatch(/white-space:\s*nowrap/);
    expect(css).toMatch(/\.quarantine-table\s*\{[^}]*overflow-wrap:\s*normal/);
    expect(css).not.toMatch(/break-all|break-word|overflow-wrap:\s*(anywhere|break-word)/);
    expect(css).not.toMatch(/text-align:\s*left/);
  });
});
