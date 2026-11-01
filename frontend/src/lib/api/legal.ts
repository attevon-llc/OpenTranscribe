import axiosInstance from '$lib/axios';

export const OPERATOR_ACK_KEY = 'opentranscribe-operator-acknowledgment';
export const TERMS_KEY = 'opentranscribe-terms-of-use';
export const PRIVACY_KEY = 'opentranscribe-privacy-notice';

export interface LegalDocumentSummary {
  key: string;
  title: string;
  version: string;
  status: string;
  audience: string;
  sha256: string;
  requires_acknowledgment: boolean;
  acknowledged: boolean;
  acknowledged_at: string | null;
  /** The organization the request acts in has accepted this text (an org admin did, for it). */
  organization_acknowledged: boolean;
  organization_acknowledged_at: string | null;
}

export type LegalAcceptanceScope = 'user' | 'organization';

export interface LegalDocument {
  key: string;
  title: string;
  version: string;
  status: string;
  sha256: string;
  markdown: string;
}

export interface LegalAcknowledgment {
  /** null when the account that accepted has since been deleted (the record is kept). */
  user_email: string | null;
  key: string;
  version: string;
  sha256: string;
  accepted_at: string;
  scope: LegalAcceptanceScope;
  organization_uuid: string | null;
}

export async function listLegalDocuments(): Promise<LegalDocumentSummary[]> {
  const { data } = await axiosInstance.get('/legal/documents');
  return Array.isArray(data) ? data : [];
}

export async function getLegalDocument(key: string): Promise<LegalDocument> {
  const { data } = await axiosInstance.get(`/legal/documents/${encodeURIComponent(key)}`);
  return data;
}

export async function acknowledgeLegalDocument(
  doc: Pick<LegalDocument, 'key' | 'version' | 'sha256'>,
  scope: LegalAcceptanceScope = 'user'
): Promise<void> {
  // `user` is the server default; only an organization acceptance names its scope.
  await axiosInstance.post('/legal/acknowledgments', {
    key: doc.key,
    version: doc.version,
    sha256: doc.sha256,
    ...(scope === 'organization' ? { scope } : {}),
  });
}

export async function listLegalAcknowledgments(): Promise<LegalAcknowledgment[]> {
  const { data } = await axiosInstance.get('/legal/acknowledgments');
  return Array.isArray(data) ? data : [];
}
