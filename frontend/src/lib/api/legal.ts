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
}

export interface LegalDocument {
  key: string;
  title: string;
  version: string;
  status: string;
  sha256: string;
  markdown: string;
}

export interface LegalAcknowledgment {
  user_email: string;
  key: string;
  version: string;
  sha256: string;
  accepted_at: string;
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
  doc: Pick<LegalDocument, 'key' | 'version' | 'sha256'>
): Promise<void> {
  await axiosInstance.post('/legal/acknowledgments', {
    key: doc.key,
    version: doc.version,
    sha256: doc.sha256,
  });
}

export async function listLegalAcknowledgments(): Promise<LegalAcknowledgment[]> {
  const { data } = await axiosInstance.get('/legal/acknowledgments');
  return Array.isArray(data) ? data : [];
}
