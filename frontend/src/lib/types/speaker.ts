/**
 * TypeScript types for Speaker-related operations
 */

/**
 * Speaker entity representing a speaker instance in a media file
 */
export interface Speaker {
  uuid: string; // Public UUID identifier
  name: string; // Original speaker ID (e.g., "SPEAKER_01")
  display_name?: string; // User-assigned display name
  verified?: boolean;
  confidence?: number;
  segment_count?: number; // Number of segments assigned to this speaker
  profile?: {
    uuid: string; // Public UUID identifier
    name: string;
    description?: string;
  } | null; // null when the speaker has been explicitly unlinked from a profile
  // AI-predicted speaker attributes
  predicted_gender?: string; // "male", "female"
  attribute_confidence?: Record<string, number | string>; // e.g., {"gender": 0.92}
  attributes_predicted_at?: string; // ISO timestamp
  // Gender alignment with metadata hints
  gender_alignment?: string; // "match" | "mismatch" | null
  gender_alignment_hint?: string; // e.g., "Joe Rogan"
  metadata_hints?: Array<{
    name: string;
    role: string;
    confidence: number;
    source: string;
  }>;
  // Backend computed fields
  computed_status?: string;
  status_text?: string;
  status_color?: string;
  resolved_display_name?: string;
}

/**
 * Transcript segment with speaker information
 */
export interface Segment {
  uuid: string; // Public UUID identifier (required)
  id?: number | string; // Legacy fallback (optional)
  start_time: number;
  end_time: number;
  text: string;
  speaker_id?: string; // UUID
  speaker_label?: string;
  resolved_speaker_name?: string;
  /**
   * The backend serialises the FULL speaker onto every segment, not just an
   * identity triple. This used to declare only `uuid`/`name`/`display_name`,
   * which is the type-level reason nothing stopped an unverified LLM guess
   * being rendered in the name slot (#741) — the suggestion fields were
   * invisible to the compiler, so reaching for a "better looking" field was
   * never flagged.
   *
   * ⚠️ `display_name` is the ONLY field meaning "a human confirmed this name":
   * `POST /speakers` and `PUT /speakers/{uuid}` both flip `verified` the moment
   * it is set. `suggested_name` + `confidence` + `suggestion_source` are the
   * machine guess and must be surfaced AS a suggestion, never as the name.
   * Note `resolved_speaker_name` above (and `Speaker.resolved_display_name`)
   * deliberately collapse the two — both come from `canonical_speaker_label()`,
   * which returns `suggested_name` once confidence >= 0.75. Do not key
   * "does this speaker have a real name" off either of them.
   */
  speaker?: {
    uuid: string; // Public UUID identifier
    name: string;
    display_name?: string;
    suggested_name?: string;
    suggestion_source?: string;
    confidence?: number;
    verified?: boolean;
  };
  formatted_timestamp?: string;
  display_timestamp?: string;
  /** ASR confidence for the segment, when the provider reports one. */
  confidence?: number;
  /**
   * Redaction spans applied to `text`, present only when the redaction pipeline
   * has run. The frontend only ever checks whether the array is non-empty (to
   * decide whether to offer the show-original toggle), so the element shape is
   * deliberately left open — see `backend/app/services/redaction/`.
   */
  redactions?: unknown[];
  // Overlap fields for simultaneous speech display
  is_overlap?: boolean;
  overlap_group_id?: string;
  overlap_confidence?: number;
  overlap_index?: number; // Position within group (computed client-side)
  overlap_count?: number; // Total in group (computed client-side)
}

/**
 * Response from the merge speakers API
 */
export interface MergeSpeakersResponse {
  uuid: string;
  name: string;
  display_name?: string;
  verified: boolean;
  segment_count: number;
}
