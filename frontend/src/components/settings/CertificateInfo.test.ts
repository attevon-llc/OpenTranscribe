import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

/**
 * The panel renders certificate metadata for a PKI-authenticated user.
 *
 * These exist because the component previously read `$userStore.certificate` —
 * a field nothing ever set, since `/api/auth/me` does not serve certificate
 * metadata and only `/api/auth/me/certificate` does. `hasCertificate` was
 * therefore permanently false and this panel silently rendered its empty state
 * for every user, including those holding a valid certificate. The first test
 * fails against that version.
 */
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((key: string) => key) };
});

const api = vi.hoisted(() => ({ getCertificateInfo: vi.fn() }));
vi.mock('$lib/api/certificate', () => api);

import CertificateInfo from './CertificateInfo.svelte';

const CERT = {
  has_certificate: true,
  subject_dn: 'CN=Ada Lovelace,O=Analytical Engines,C=GB',
  common_name: 'Ada Lovelace',
  serial_number: '0A1B2C',
  issuer_dn: 'CN=Test CA',
  organization: 'Analytical Engines',
  organizational_unit: 'Research',
  valid_from: '2026-01-01T00:00:00Z',
  valid_until: '2099-01-01T00:00:00Z',
  fingerprint: 'AA:BB:CC',
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getCertificateInfo.mockResolvedValue(CERT);
});

describe('CertificateInfo', () => {
  it('fetches the certificate endpoint and renders what it returns', async () => {
    render(CertificateInfo);

    await waitFor(() => expect(api.getCertificateInfo).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByText('CN=Ada Lovelace,O=Analytical Engines,C=GB')).toBeInTheDocument()
    );
  });

  it('renders nothing when the user holds no certificate', async () => {
    api.getCertificateInfo.mockResolvedValue({ has_certificate: false });
    const { container } = render(CertificateInfo);

    await waitFor(() => expect(api.getCertificateInfo).toHaveBeenCalled());
    expect(container.querySelector('.certificate-info')).toBeNull();
  });

  it('degrades quietly when the request fails', async () => {
    // The rest of the security panel is unaffected by this one call, so a
    // failure must not take the page down with it.
    api.getCertificateInfo.mockRejectedValue(new Error('boom'));
    const { container } = render(CertificateInfo);

    await waitFor(() => expect(api.getCertificateInfo).toHaveBeenCalled());
    expect(container.querySelector('.certificate-info')).toBeNull();
  });
});
