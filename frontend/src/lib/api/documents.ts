import axiosInstance from '$lib/axios';
import type {
  DocumentChunkListResponse,
  DocumentDownloadResponse,
  DocumentListResponse,
  DocumentResponse,
} from '$lib/types/document';

export interface ListDocumentsOptions {
  search?: string;
  status?: string[];
  sortBy?: 'created_at' | 'filename' | 'file_size' | 'word_count' | 'parsed_at';
  sortOrder?: 'asc' | 'desc';
}

export async function listDocuments(
  skip = 0,
  limit = 50,
  options: ListDocumentsOptions = {}
): Promise<DocumentListResponse> {
  const response = await axiosInstance.get('/documents', {
    params: {
      skip,
      limit,
      search: options.search || undefined,
      status: options.status && options.status.length > 0 ? options.status : undefined,
      sort_by: options.sortBy,
      sort_order: options.sortOrder,
    },
    // Axios's default array serialization is `status[]=completed`, which FastAPI's
    // `status: list[str] | None = Query(None)` does not recognize — it needs the
    // param repeated bare (`status=completed&status=x`). Without this the `status`
    // filter is silently a no-op: verified live, PickerDocumentsTab's "completed
    // only" request returned every document regardless of status until this was
    // added. Same fix already applied in `$lib/prefetch.ts` for `/search`.
    paramsSerializer: (params: Record<string, unknown>) => {
      const sp = new URLSearchParams();
      Object.entries(params).forEach(([key, value]) => {
        if (value === undefined) return;
        if (Array.isArray(value)) {
          value.forEach((v) => sp.append(key, String(v)));
        } else {
          sp.set(key, String(value));
        }
      });
      return sp.toString();
    },
  });
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

// Retry a failed (or stuck) parse. Owner or admin; resets status to `pending` and
// re-dispatches `documents.parse` server-side. See backend/app/api/endpoints/documents.py.
export async function reparseDocument(uuid: string): Promise<DocumentResponse> {
  const response = await axiosInstance.post(`/documents/${uuid}/reparse`);
  return response.data;
}
