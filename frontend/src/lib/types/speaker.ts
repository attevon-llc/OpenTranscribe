/**
 * TypeScript types for Speaker-related operations
 */

/**
 * How a speaker's CURRENT identity came to be (read-only; backend `SpeakerIdentity`).
 *
 * `by` is present only for the file's owner, an administrator of the file's organization and a
 * platform administrator; any other viewer gets `source` and `at`. The whole object is `null`
 * when the origin is unknown (written before it was recorded) or withheld because a voiceprint
 * made it and the file's voiceprints are off. Only the current state is kept; the change
 * history is a later release.
 */
export interface SpeakerIdentity {
  source: 'human' | 'voiceprint' | 'voiceprint_link';
  at?: string | null; // ISO timestamp
  by?: { uuid: string; name?: string | null } | null;
  detail?: Record<string, unknown> | null; // ids and identifiers only, never a name
}

/** Where the CURRENT recommendation in `suggested_name` came from (backend `SpeakerSuggestion`). */
export interface SpeakerSuggestion {
  source?: string | null; // "voice_match" | "profile_match" | "llm_analysis" | ...
  confidence?: number | null;
  detail?: Record<string, unknown> | null;
}

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
  // Numeric scores only, e.g. {"gender": 0.92}. The backend splits the non-numeric
  // keys it stores alongside them into the three fields below (#1026).
  attribute_confidence?: Record<string, number> | null;
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
  // Read-only provenance of the identity and of the recommendation
  identity?: SpeakerIdentity | null;
  suggestion?: SpeakerSuggestion | null;
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
   * come from `canonical_speaker_label()`, which returns a typed `display_name`
   * or a VOICEPRINT-matched `suggested_name` (`suggestion_source` `voice_match` /
   * `profile_match`, confidence >= 0.75) — never an LLM one (`llm_analysis`).
   * They still mix "confirmed" and "matched by voice", so do not key
   * "has a human confirmed a name" off either of them.
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
