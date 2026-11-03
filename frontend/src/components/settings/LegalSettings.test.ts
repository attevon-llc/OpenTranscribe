import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

import LegalSettings from './LegalSettings.svelte';
import type { LegalDocumentSummary } from '$lib/api/legal';

const docs: LegalDocumentSummary[] = [
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
    organization_acknowledged: false,
    organization_acknowledged_at: null,
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
    organization_acknowledged: false,
    organization_acknowledged_at: null,
  },
];

const gate = {
  required_document_keys: [] as string[],
  acceptance_scope: 'either',
  enabled: false,
  available_documents: [
    { key: 'opentranscribe-terms-of-use', title: 'Terms of Use', version: '1.0' },
    { key: 'opentranscribe-privacy-notice', title: 'Privacy Notice', version: '1.0' },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.put.mockImplementation(async (_url: string, body: Record<string, unknown>) => ({
    data: { ...gate, ...body, enabled: true },
  }));
  mockAxios.get.mockImplementation(async (url: string) => ({
    data:
      url === '/legal/documents'
        ? docs
        : url === '/admin/legal-processing-gate'
          ? gate
          : [
              {
                user_email: 'op@example.com',
                key: docs[0].key,
                version: '1.0',
                sha256: 'a'.repeat(64),
                accepted_at: '2026-10-01T00:00:00Z',
              },
              {
                user_email: null,
                key: docs[1].key,
                version: '1.0',
                sha256: 'b'.repeat(64),
                accepted_at: '2026-09-01T00:00:00Z',
                scope: 'user',
                organization_uuid: null,
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

  it('shows the processing requirement only to super admins', async () => {
    render(LegalSettings, { isAdmin: true, isSuperAdmin: false });
    expect(await screen.findByTestId('legal-ack-table')).toBeTruthy();
    expect(screen.queryByTestId('legal-processing-gate')).toBeNull();
    expect(mockAxios.get).not.toHaveBeenCalledWith('/admin/legal-processing-gate');
  });

  it('saves the selected documents and scope', async () => {
    render(LegalSettings, { isAdmin: true, isSuperAdmin: true });

    expect(await screen.findByTestId('legal-gate-off')).toBeTruthy();
    const save = screen.getByTestId('legal-gate-save') as HTMLButtonElement;
    expect(save.disabled).toBe(true); // nothing changed yet
    const boxes = screen.getAllByTestId('legal-gate-doc') as HTMLInputElement[];
    expect(boxes.map((b) => b.value)).toEqual([
      'opentranscribe-terms-of-use',
      'opentranscribe-privacy-notice',
    ]);
    await fireEvent.click(boxes[0]);
    await fireEvent.click(screen.getByLabelText('legal.processingGate.scopeUser'));
    expect(save.disabled).toBe(false);
    await fireEvent.click(save);

    expect(mockAxios.put).toHaveBeenCalledWith('/admin/legal-processing-gate', {
      required_document_keys: ['opentranscribe-terms-of-use'],
      acceptance_scope: 'user',
    });
    await vi.waitFor(() => expect(screen.queryByTestId('legal-gate-off')).toBeNull());
  });

  it('labels a record whose account was deleted instead of showing a blank user', async () => {
    render(LegalSettings, { isAdmin: true, isSuperAdmin: false });

    expect((await screen.findByTestId('legal-erased')).textContent?.trim()).toBe(
      'legal.retention.erasedRecord'
    );
  });

  it('hides organization acceptance from a user who is not an org admin', async () => {
    render(LegalSettings, { isAdmin: true, isSuperAdmin: true, isOrgAdmin: false });

    expect((await screen.findAllByText('Terms of Use')).length).toBeGreaterThan(0);
    expect(screen.queryByTestId('legal-org-acceptance')).toBeNull();
  });

  it('lets an org admin accept for the organization, never the operator text', async () => {
    mockAxios.post.mockResolvedValue({ data: {} });
    render(LegalSettings, { isOrgAdmin: true });

    const buttons = await screen.findAllByTestId('legal-org-accept-btn');
    expect(buttons).toHaveLength(1); // the operator acknowledgment is not org-scoped
    await fireEvent.click(buttons[0]);

    expect(mockAxios.post).toHaveBeenCalledWith('/legal/acknowledgments', {
      key: 'opentranscribe-terms-of-use',
      version: '1.0',
      sha256: 'b'.repeat(64),
      scope: 'organization',
    });
    await waitFor(() =>
      expect(mockAxios.get.mock.calls.filter(([u]) => u === '/legal/documents')).toHaveLength(2)
    );
  });

  it('shows the accepted state and the refusal message', async () => {
    docs[1].organization_acknowledged = true;
    docs[1].organization_acknowledged_at = '2026-10-02T00:00:00Z';
    try {
      render(LegalSettings, { isOrgAdmin: true });
      expect((await screen.findByTestId('legal-org-accepted')).textContent).toContain(
        'legal.orgAcceptance.acceptedOn'
      );
      expect(screen.queryByTestId('legal-org-accept-btn')).toBeNull();
    } finally {
      docs[1].organization_acknowledged = false;
      docs[1].organization_acknowledged_at = null;
    }
  });

  it('maps a forbidden refusal to its own message', async () => {
    mockAxios.post.mockRejectedValue({
      response: { data: { detail: { code: 'legal_org_acceptance_forbidden' } } },
    });
    render(LegalSettings, { isOrgAdmin: true });

    await fireEvent.click((await screen.findAllByTestId('legal-org-accept-btn'))[0]);
    expect((await screen.findByRole('alert')).textContent).toBe(
      'legal.errors.orgAcceptanceForbidden'
    );
  });
});
