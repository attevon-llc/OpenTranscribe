import axiosInstance from '$lib/axios';
import type {
  DocumentChunkListResponse,
  DocumentDownloadResponse,
  DocumentListResponse,
  DocumentResponse,
} from '$lib/types/document';

export async function listDocuments(skip = 0, limit = 50): Promise<DocumentListResponse> {
  const response = await axiosInstance.get('/documents', { params: { skip, limit } });
  return response.data;
}

export async function getDocument(uuid: string): Promise<DocumentResponse> {
  const response = await axiosInstance.get(`/documents/${uuid}`);
  return response.data;
}

export async function getDocumentChunks(uuid: string): Promise<DocumentChunkListResponse> {
  const response = await axiosInstance.get(`/documents/${uuid}/chunks`);
  return response.data;
}

// `download=false` (the default) is safe to point an <iframe> at — no Content-Disposition
// override, so a PDF renders instead of downloading. Pass `download: true` for a format
// nothing in-browser can render (DOCX/PPTX/XLSX and friends).
export async function getDocumentDownloadUrl(
  uuid: string,
  download = false
): Promise<DocumentDownloadResponse> {
  const response = await axiosInstance.get(`/documents/${uuid}/download`, {
    params: { download },
  });
  return response.data;
}

export async function deleteDocument(uuid: string): Promise<void> {
  await axiosInstance.delete(`/documents/${uuid}`);
}
