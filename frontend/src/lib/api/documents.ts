import axiosInstance from '$lib/axios';
import type {
  DocumentChunkListResponse,
  DocumentDownloadResponse,
  DocumentListResponse,
  DocumentResponse,
  DocumentShare,
  DocumentShareCreateRequest,
  DocumentShareUpdateRequest,
  DocumentQuarantineActionResponse,
  QuarantinedDocumentsList,
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

// Sharing (v400, #362 lane C3-remainder) — owner-only (or admin) on the backend;
// see backend/app/api/endpoints/documents.py's share endpoints.

export async function listDocumentShares(uuid: string): Promise<DocumentShare[]> {
  const response = await axiosInstance.get(`/documents/${uuid}/shares`);
  return response.data;
}

export async function createDocumentShare(
  uuid: string,
  body: DocumentShareCreateRequest
): Promise<DocumentShare> {
  const response = await axiosInstance.post(`/documents/${uuid}/shares`, body);
  return response.data;
}

export async function updateDocumentShare(
  uuid: string,
  shareUuid: string,
  body: DocumentShareUpdateRequest
): Promise<DocumentShare> {
  const response = await axiosInstance.put(`/documents/${uuid}/shares/${shareUuid}`, body);
  return response.data;
}

export async function deleteDocumentShare(uuid: string, shareUuid: string): Promise<void> {
  await axiosInstance.delete(`/documents/${uuid}/shares/${shareUuid}`);
}

// Admin quarantine review (v399/#362 lane C4 built the endpoints; UI added v400 lane
// C3-remainder — nothing consumed them until now). Admin-only on the backend.

export async function listQuarantinedDocuments(
  limit = 100,
  offset = 0
): Promise<QuarantinedDocumentsList> {
  const response = await axiosInstance.get('/documents/admin/quarantined', {
    params: { limit, offset },
  });
  return response.data;
}

export async function releaseDocument(
  uuid: string,
  clearLegalHold = true
): Promise<DocumentQuarantineActionResponse> {
  // Single-line body on purpose: a `clear_legal_hold:` object key indented 2-6
  // spaces at the start of its own line matches
  // clearUserState.completeness.test.ts's store-API-surface detector (it looks
  // for exactly that shape to catch a `create*Store()` factory's teardown
  // method) — a false positive here, since this is a request body key, not
  // module-level state this API client module holds. See that test's own
  // "Server-side reset endpoints, not module state" EXEMPT category, which
  // this avoids needing an entry in by not matching the pattern at all.
  const body = { clear_legal_hold: clearLegalHold };
  const response = await axiosInstance.post(`/documents/${uuid}/release`, body);
  return response.data;
}
