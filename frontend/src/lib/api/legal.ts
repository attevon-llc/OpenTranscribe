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

/** Whose acceptance satisfies the processing requirement (server-validated). */
export type ProcessingAcceptanceScope = 'user' | 'organization' | 'either';

export interface ProcessingGateSettings {
  required_document_keys: string[];
  acceptance_scope: ProcessingAcceptanceScope;
  enabled: boolean;
  available_documents: { key: string; title: string; version: string }[];
}

export async function getProcessingGateSettings(): Promise<ProcessingGateSettings> {
  const { data } = await axiosInstance.get('/admin/legal-processing-gate');
  return data;
}

export async function updateProcessingGateSettings(
  changes: Partial<Pick<ProcessingGateSettings, 'required_document_keys' | 'acceptance_scope'>>
): Promise<ProcessingGateSettings> {
  const { data } = await axiosInstance.put('/admin/legal-processing-gate', changes);
  return data;
}
