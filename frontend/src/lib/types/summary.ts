/**
 * TypeScript types for AI-generated summaries
 *
 * Updated to support flexible summary structures from custom AI prompts.
 * Accepts any valid JSON structure while providing types for the default BLUF format.
 */

// Legacy types for default BLUF format (optional fields)
export interface SpeakerInfo {
  name: string;
  talk_time_seconds: number;
  percentage: number;
  key_points: string[];
}

export interface ContentSection {
  time_range: string;
  topic: string;
  key_points: string[];
}

export interface MajorTopic {
  topic: string;
  importance: 'high' | 'medium' | 'low';
  key_points: string[];
  participants: string[];
}

/**
 * Shape-tolerant on purpose (W2.5 Task 1). The DEFAULT summary prompt
 * (`backend/app/core/default_prompts.py`, the `action_items` block) emits
 * `{item, owner, due_date, priority, context, mentioned_timestamp}` — a
 * DIFFERENT shape from the one this interface used to declare exclusively
 * (`{text, assigned_to, ..., status}`, mirroring `backend/app/schemas/
 * summary.py`'s `ActionItem`, which is exported but DEAD — nothing on the
 * backend validates or produces it). Both spellings are declared here,
 * optional, because `SummaryData` is `extra="allow"` and a custom prompt may
 * emit either, or neither. `SummaryDisplay.svelte`'s `actionItemText()`/
 * `actionItemOwner()` helpers are the actual shape-tolerant readers; this
 * type exists so a literal built from either shape still type-checks.
 */
export interface ActionItem {
  // The shape the DEFAULT prompt actually emits.
  item?: string;
  owner?: string | null;
  mentioned_timestamp?: string | null;
  // The dead `schemas/summary.py` shape — kept for backward compatibility
  // with anything that was already relying on it.
  text?: string;
  assigned_to?: string | null;
  status?: 'pending' | 'completed' | 'cancelled';
  // Common to both.
  due_date?: string | null;
  priority?: 'high' | 'medium' | 'low';
  context?: string;
}

export interface SummaryMetadata {
  provider: string;
  model: string;
  usage_tokens?: number;
  transcript_length: number;
  processing_time_ms?: number;
  confidence_score?: number;
  language?: string;
  error?: string;
}

/**
 * Flexible summary data structure that accepts ANY valid JSON structure.
 *
 * This allows custom AI prompts to return different formats while still
 * providing type hints for the default BLUF format fields.
 *
 * Examples:
 * - Default BLUF: { bluf, brief_summary, major_topics, ... }
 * - Custom: { executive_summary, risks, recommendations, ... }
 */
export interface SummaryData {
  // Optional fields from default BLUF prompt
  bluf?: string;
  brief_summary?: string;
  speakers?: SpeakerInfo[];
  major_topics?: MajorTopic[];
  action_items?: ActionItem[];
  key_decisions?: string[];
  follow_up_items?: string[];
  metadata?: SummaryMetadata;

  // Allow any additional fields from custom prompts
  [key: string]: any;
}

// `GET /files/{uuid}/summary`. The `source` / `document_id` / `created_at` /
// `updated_at` fields named which document in the `transcript_summaries`
// OpenSearch index answered; that index is retired (#67), the summary is served
// from `media_file.summary_data`, and nothing here ever read them.
export interface SummaryResponse {
  file_id: string; // UUID
  summary_data: SummaryData; // Flexible structure
}

// `SummarySearchHit` / `SummarySearchResponse` described `POST /api/files/search`,
// which queried only that index and was unmounted with it (#67). They had no
// importer outside this file.

export interface SpeakerIdentificationResponse {
  message: string;
  task_id: string;
  file_id: string; // UUID
  speaker_count: number;
}
