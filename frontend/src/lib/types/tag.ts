/**
 * Canonical tag shape, mirroring `backend/app/schemas/media.py:Tag`.
 *
 * Import via `$lib/types/tag`. Note `MediaFile.tags` is a `string[]` (the
 * backend flattens tags to names on the file payload) — this object shape is
 * what the `/tags` endpoints and the tag editor/filter surfaces return.
 */
export interface Tag {
  uuid: string;
  name: string;
  /** How the tag was created — e.g. `auto_ai` for LLM-suggested tags. */
  source?: string;
  /** Number of files carrying this tag; only present on listing endpoints. */
  usage_count?: number;
}
