/**
 * `getCertificateInfo` is a single thin GET wrapper around
 * `/auth/me/certificate` — pins the request path and response pass-through,
 * including the "no certificate" shape non-PKI users get back.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock('../axios', () => ({ default: mockInstance }));

import { getCertificateInfo } from './certificate';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getCertificateInfo', () => {
  it('fetches certificate metadata for a PKI-authenticated user', async () => {
    const info = {
      has_certificate: true,
      subject_dn: 'CN=Jane Doe',
      common_name: 'Jane Doe',
      serial_number: '01A2',
      issuer_dn: 'CN=Acme CA',
      organization: 'Acme',
      organizational_unit: 'Eng',
      valid_from: '2026-01-01T00:00:00Z',
      valid_until: '2027-01-01T00:00:00Z',
      fingerprint: 'ab:cd:ef',
    };
    mockInstance.get.mockResolvedValue({ data: info });

    const result = await getCertificateInfo();
    expect(mockInstance.get).toHaveBeenCalledWith('/auth/me/certificate');
    expect(result).toEqual(info);
  });

  it('returns has_certificate: false for a non-PKI user', async () => {
    mockInstance.get.mockResolvedValue({ data: { has_certificate: false } });

    const result = await getCertificateInfo();
    expect(result).toEqual({ has_certificate: false });
  });
});
