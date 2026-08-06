/**
 * Canonical tag shape, mirroring `backend/app/schemas/media.py:Tag`.
 *
 * Import via `$lib/types/tag`. This is the **only** tag shape on the API:
 * `/tags`, `POST /tags/files/{uuid}/tags` and `MediaFileDetail.tags` all serve
 * it (#326). The gallery list endpoint `GET /files` sends no tags at all, which
 * is why `MediaFile` has no `tags` field — only `MediaFileDetail` does.
 */
export interface Tag {
  uuid: string;
  name: string;
  /** How the tag was created — e.g. `auto_ai` for LLM-suggested tags. */
  source?: string;
  /** Number of files carrying this tag; only present on listing endpoints. */
  usage_count?: number;
}
