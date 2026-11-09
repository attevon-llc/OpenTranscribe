/**
 * TypeScript types for Speaker-related operations
 */

/**
 * How a speaker's CURRENT identity came to be (read-only; backend `SpeakerIdentity`).
 *
 * `by` is present only for the file's owner, an administrator of the file's organization and a
 * platform administrator; any other viewer gets `source` and `at`. The whole object is `null`
 * when the origin is unknown (written before it was recorded) or withheld because a voiceprint
 * made it and the file's voiceprints are off. This is the current state; every change is in
 * the speaker's identity history (`SpeakerIdentityEvent`).
 */
interface SpeakerIdentity {
  source: 'human' | 'voiceprint' | 'voiceprint_link';
  at?: string | null; // ISO timestamp
  by?: { uuid: string; name?: string | null } | null;
  detail?: Record<string, unknown> | null; // ids and identifiers only, never a name
}

/** The kinds of change the identity history records (backend `EVENT_KINDS`). */
type SpeakerIdentityEventKind =
  | 'rename'
  | 'name_cleared'
  | 'link'
  | 'unlink'
  | 'suggested'
  | 'suggestion_accepted'
  | 'suggestion_rejected'
  | 'suggestion_cleared'
  | 'confirmed'
  | 'skipped'
  | 'merge'
  | 'purge';

/** The fields one change touched, before or after it (only those it touched are present). */
export interface SpeakerIdentityEventValues {
  name?: string | null;
  profile?: { uuid: string | null; name: string | null; deleted?: boolean } | null;
  suggested_name?: string | null;
  suggestion_source?: string | null;
  identity_source?: string | null;
  merged_name?: string | null;
  /** Fields whose previous value a voiceprint wrote; it is kept only in the voiceprint's entry. */
  withheld?: string[];
}

/** One entry of a speaker's identity history (backend `SpeakerIdentityEvent`, issue #1336). */
export interface SpeakerIdentityEvent {
  uuid: string;
  kind: SpeakerIdentityEventKind;
  at: string; // ISO timestamp
  actor: {
    kind: 'user' | 'system';
    // Named only for the viewers who see `identity.by`; null otherwise.
    user?: { uuid: string | null; name?: string | null; erased?: boolean } | null;
  };
  origin?: string | null;
  model?: string | null;
  confidence?: number | null;
  before?: SpeakerIdentityEventValues | null;
  after?: SpeakerIdentityEventValues | null;
}

/** Where the CURRENT recommendation in `suggested_name` came from (backend `SpeakerSuggestion`). */
interface SpeakerSuggestion {
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
 * Response from the merge speakers API
 */
export interface MergeSpeakersResponse {
  uuid: string;
  name: string;
  display_name?: string;
  verified: boolean;
  segment_count: number;
}
