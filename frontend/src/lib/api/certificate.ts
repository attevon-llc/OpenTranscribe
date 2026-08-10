/**
 * API client for the current user's X.509 certificate metadata.
 *
 * `GET /api/auth/me/certificate` is the **only** source of this data — the user
 * payload (`/api/auth/me`) does not carry it, because the fields are meaningless
 * for the local/LDAP/OIDC majority and would be dead weight on every session
 * probe.
 */
import axiosInstance from '../axios';

/** Certificate metadata for a PKI-authenticated user. */
export interface CertificateInfo {
  has_certificate: boolean;
  subject_dn?: string | null;
  common_name?: string | null;
  serial_number?: string | null;
  issuer_dn?: string | null;
  organization?: string | null;
  organizational_unit?: string | null;
  valid_from?: string | null;
  valid_until?: string | null;
  fingerprint?: string | null;
}

/**
 * Fetch the caller's certificate metadata.
 *
 * Returns `{ has_certificate: false }` for a user who did not authenticate via
 * PKI (or via an OIDC provider brokering X.509), so callers render the absence
 * rather than treating it as an error.
 */
export async function getCertificateInfo(): Promise<CertificateInfo> {
  const response = await axiosInstance.get('/auth/me/certificate');
  return response.data;
}
