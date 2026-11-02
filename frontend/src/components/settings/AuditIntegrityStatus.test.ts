/**
 * Audit record protection status (super_admin). The server measures it; the component only
 * renders the answer, and renders nothing when the read fails.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

const api = vi.hoisted(() => ({ getAuditIntegrity: vi.fn() }));
vi.mock('$lib/api/admin', () => ({ AdminApi: api }));

import AuditIntegrityStatus from './AuditIntegrityStatus.svelte';

const base = {
  split_requested: false,
  enforced: false,
  runtime_role_is_superuser: true,
  runtime_role_owns_tables: true,
  tables: [],
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('AuditIntegrityStatus', () => {
  it('says enforced only when the server says enforced', async () => {
    api.getAuditIntegrity.mockResolvedValue({ ...base, split_requested: true, enforced: true });
    render(AuditIntegrityStatus);

    const state = await screen.findByTestId('audit-integrity-state');
    expect(state.textContent).toContain('Enforced:');
    expect(state.textContent).not.toContain('Not enforced');
  });

  it('says it is only an accident guard when the application is the owner', async () => {
    api.getAuditIntegrity.mockResolvedValue(base);
    render(AuditIntegrityStatus);

    const state = await screen.findByTestId('audit-integrity-state');
    expect(state.textContent).toContain('Not enforced');
    expect(screen.getByRole('link').getAttribute('href')).toContain('audit-integrity');
  });

  it('renders nothing when the status cannot be read', async () => {
    api.getAuditIntegrity.mockRejectedValue(new Error('403'));
    const { container } = render(AuditIntegrityStatus);

    await waitFor(() => expect(api.getAuditIntegrity).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="audit-integrity-status"]')).toBeNull();
  });
});
