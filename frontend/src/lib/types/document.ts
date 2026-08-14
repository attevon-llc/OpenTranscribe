// Mirrors backend/app/schemas/document.py exactly — no codegen in this repo (see
// backend/app/schemas/CLAUDE.md), so this file is hand-kept in sync.

export interface DocumentResponse {
  uuid: string;
  filename: string;
  file_size: number;
  content_type: string;
  status: string;
  display_status: string;
  error_category: string | null;
  last_error_message: string | null;
  parser: string | null;
  page_count: number | null;
  word_count: number;
  chunk_count: number;
  language: string | null;
  has_embedded_text: boolean | null;
  ocr_applied: boolean;
  parse_warnings: string[];
  created_at: string;
  updated_at: string;
  parsed_at: string | null;
}

export interface DocumentListResponse {
  documents: DocumentResponse[];
  total: number;
  skip: number;
  limit: number;
}

export interface DocumentChunkResponse {
  chunk_index: number;
  text: string;
  char_start: number;
  char_end: number;
  page: number | null;
  section_path: string[] | null;
  block_types: string[] | null;
}

export interface DocumentChunkListResponse {
  chunks: DocumentChunkResponse[];
  total: number;
}

export interface DocumentDownloadResponse {
  url: string;
  filename: string;
  content_type: string;
}

// Formats a browser can render natively in an <iframe> — everything else falls back
// to a download-only original view. Keep in sync with the backend's DOCUMENT_MIME_TYPES
// if that set ever narrows (services/documents/detect.py).
export const NATIVELY_RENDERABLE_TYPES = new Set([
  'application/pdf',
  'text/html',
  'text/plain',
  'text/markdown',
  'text/csv',
]);

export function isNativelyRenderable(contentType: string): boolean {
  return NATIVELY_RENDERABLE_TYPES.has(contentType);
}
