/**
 * Canonical collection shape, mirroring `backend/app/schemas/collection.py`.
 *
 * Before #284 A3.6 there were three competing local declarations (one exported
 * from `components/collections/CollectionsList.svelte`, two anonymous ones) plus
 * a handful of `any[]`. This is the single home — import it via
 * `$lib/types/collection`.
 *
 * NOT the same entity as `SharedCollection` in `$lib/types/groups`: that is the
 * shared-with-me projection (`my_permission`, `shared_by`, …), this is the
 * collection the caller owns.
 */
export interface Collection {
  uuid: string;
  name: string;
  description?: string | null;
  /** Number of media files in the collection. Absent on some create/update responses. */
  media_count?: number;
  is_public?: boolean;
  /** How the collection came to exist — e.g. `auto_ai`, `bulk_group`. */
  source?: string;
  default_prompt_name?: string | null;
  default_prompt_id?: string | null;
  share_count?: number;
  /** True if shared with (not owned by) the caller. Mirrors `CollectionWithCount.is_shared`. */
  is_shared?: boolean;
  /** Caller's effective permission — `'owner' | 'editor' | 'viewer'`. Defaults to `'owner'` server-side. */
  my_permission?: string;
  /**
   * Who in the organization sees it (issue #1275): every member (`organization`) or only the
   * creator, organization admins and the people it is shared with (`restricted`). A personal
   * collection is always `organization`, which has no meaning there.
   */
  visibility?: CollectionVisibility;
  /** Server-computed: the caller may switch `visibility`. Never re-derive it in the UI. */
  can_change_visibility?: boolean;
}

export type CollectionVisibility = 'organization' | 'restricted';
