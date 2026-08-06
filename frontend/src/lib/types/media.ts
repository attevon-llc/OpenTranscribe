/**
 * Shared media file types used across gallery components.
 */
import type { Tag } from '$lib/types/tag';

/**
 * Every value of the backend `FileStatus` enum (`backend/app/core/enums.py`).
 *
 * Hand-maintained mirror — there is no codegen and no `openapi-typescript`, so nothing
 * detects drift but `media.status.test.ts`, which pins this list. Add new statuses to
 * both, and audit `switch`/`if` chains over status when you do.
 */
export type MediaFileStatus =
  | 'pending'
  | 'queued'
  | 'downloading'
  | 'processing'
  | 'completed'
  | 'error'
  | 'cancelling'
  | 'cancelled'
  | 'orphaned'
  | 'quarantined';

export interface MediaFile {
  uuid: string;
  filename: string;
  status: MediaFileStatus;
  upload_time: string;
  duration?: number;
  file_size?: number;
  content_type?: string;
  summary?: string;
  file_hash?: string;
  thumbnail_url?: string;
  last_error_message?: string;

  // Formatted fields from backend
  formatted_duration?: string;
  formatted_upload_date?: string;
  formatted_file_age?: string;
  formatted_file_size?: string;
  display_status?: string;
  status_badge_class?: string;

  // Error handling fields from backend
  error_category?: string;
  error_suggestions?: string[];
  user_message?: string;
  is_retryable?: boolean;

  // Technical metadata
  media_format?: string;
  codec?: string;
  resolution_width?: number;
  resolution_height?: number;
  frame_rate?: number;
  frame_count?: number;
  aspect_ratio?: string;

  // Audio specs
  audio_channels?: number;
  audio_sample_rate?: number;
  audio_bit_depth?: number;

  // Creation info
  creation_date?: string;
  last_modified_date?: string;
  device_make?: string;
  device_model?: string;

  // Content info
  title?: string;
  author?: string;
  description?: string;
  language?: string;

  // Speaker summary from backend
  speaker_summary?: {
    count: number;
    primary_speakers: string[];
  };

  // Diarization
  diarization_disabled?: boolean;
}

/**
 * What `GET /files/{uuid}` actually returns — `MediaFile` plus everything the
 * detail page needs and the gallery list does not.
 *
 * `MediaFile` stays the gallery/list shape on purpose (see this folder's
 * CLAUDE.md); this extends it rather than widening it, so a list component
 * can't silently start depending on a field the list endpoint never sends.
 */
export interface MediaFileDetail extends MediaFile {
  /**
   * Full tag objects, the same shape `/api/tags` returns (#326). `GET /files`
   * sends no tags, so this deliberately lives here and not on `MediaFile`.
   */
  tags?: Tag[];
  transcript_segments?: unknown[];
  grouped_segments?: GroupedTranscriptSegment[];
  total_segments?: number;
  speakers?: unknown[];
  collections?: unknown[];
  analytics?: Record<string, unknown> | null;

  // Summary / enrichment
  has_summary?: boolean;
  summary_opensearch_id?: string | null;
  summary_status?: string | null;

  // Model provenance (rendered by MetadataDisplay)
  asr_model?: string;
  asr_provider?: string;
  whisper_model?: string;
  diarization_model?: string;
  embedding_mode?: string;

  // Source / delivery
  source_url?: string;
  download_url?: string;

  // In-flight processing state
  progress?: number;
  error_message?: string;
}

/**
 * Backend-shaped overlap-grouped transcript segment (snake_case), present on both
 * the file-detail payload and `GET /files/{uuid}/segments` as `grouped_segments`.
 * Mirrors the Pydantic `GroupedTranscriptSegment` schema. The backend owns grouping;
 * the frontend never recomputes it.
 *
 * Groups reference their segments by UUID — **never** embed copies. `transcript_segments`
 * is the single representation of segment data; a second copy inside groups meant every
 * optimistic update patched only one of them and the transcript rendered stale (#352).
 * `TranscriptDisplay.mapBackendGroup` is the one place that resolves these references.
 *
 * `start_segment_index` is global across pagination, and `overlap_group_id` is set on
 * single-member groups too so a run split across a page boundary can be stitched.
 */
export interface GroupedTranscriptSegment {
  is_overlap_group: boolean;
  overlap_group_id?: string | null;
  start_time: number;
  end_time: number;
  start_segment_index: number;
  segment_uuids: string[];
}

/**
 * Client-side camelCase view of {@link GroupedTranscriptSegment}.
 *
 * `TranscriptDisplay` maps the backend payload into this shape (and falls back to
 * computing it locally for older payloads that predate `grouped_segments`), then
 * passes it to `TranscriptSegmentList`. It lives here so the coordinator and the
 * child can't drift — it used to be declared privately in `TranscriptDisplay` while
 * the child typed the same prop `any[]`.
 *
 * `segments` stays open for the same reason `GroupedTranscriptSegment.segments` does.
 */
export interface GroupedSegmentView {
  isOverlapGroup: boolean;
  overlapGroupId?: string;
  startTime: number;
  endTime: number;
  segments: any[];
  /** Index of the first segment in the flat array — drives reading progress. */
  startSegmentIndex: number;
}

export interface DurationRange {
  min: number | null;
  max: number | null;
}

export interface DateRange {
  from: Date | null;
  to: Date | null;
}
