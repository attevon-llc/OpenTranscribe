import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import LegalSettings from './LegalSettings.svelte';

const docs = [
  {
    key: 'opentranscribe-operator-acknowledgment',
    title: 'Operator acknowledgment',
    version: '1.0',
    status: 'draft',
    audience: 'super_admin',
    sha256: 'a'.repeat(64),
    requires_acknowledgment: true,
    acknowledged: true,
    acknowledged_at: '2026-10-01T00:00:00Z',
  },
  {
    key: 'opentranscribe-terms-of-use',
    title: 'Terms of Use',
    version: '1.0',
    status: 'final',
    audience: 'all',
    sha256: 'b'.repeat(64),
    requires_acknowledgment: false,
    acknowledged: false,
    acknowledged_at: null,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockImplementation(async (url: string) => ({
    data:
      url === '/legal/documents'
        ? docs
        : [
            {
              user_email: 'op@example.com',
              key: docs[0].key,
              version: '1.0',
              sha256: 'a'.repeat(64),
              accepted_at: '2026-10-01T00:00:00Z',
            },
          ],
  }));
});

describe('LegalSettings', () => {
  it('lists documents with a draft badge only on the draft one', async () => {
    render(LegalSettings, { isAdmin: false, isSuperAdmin: false });

    expect(await screen.findByText('Terms of Use')).toBeTruthy();
    const badges = screen.getAllByTestId('legal-status').map((b) => b.textContent?.trim());
    expect(badges).toEqual(['legal.draftBadge', 'final']);
    expect(screen.queryByTestId('legal-ack-table')).toBeNull();
    expect(screen.queryByTestId('legal-own-ack')).toBeNull();
    expect(mockAxios.get).not.toHaveBeenCalledWith('/legal/acknowledgments');
  });

  it('shows the acknowledgment table to admins and own state to super admins', async () => {
    render(LegalSettings, { isAdmin: true, isSuperAdmin: true });

    expect(await screen.findByTestId('legal-ack-table')).toBeTruthy();
    expect(screen.getByText('op@example.com')).toBeTruthy();
    expect(screen.getByTestId('legal-own-ack').textContent).toContain(
      'legal.settings.ownAcknowledged'
    );
  });
});
