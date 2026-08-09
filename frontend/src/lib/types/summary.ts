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

export interface ActionItem {
  text: string;
  assigned_to: string | null;
  due_date: string | null;
  priority: 'high' | 'medium' | 'low';
  context: string;
  status?: 'pending' | 'completed' | 'cancelled';
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

export interface SummaryResponse {
  file_id: string; // UUID
  summary_data: SummaryData; // Flexible structure
  source: 'opensearch' | 'postgresql';
  document_id?: string;
  created_at?: string;
  updated_at?: string;
}

export interface SummarySearchHit {
  document_id: string;
  score: number;
  file_id: string; // UUID
  bluf: string;
  brief_summary: string;
  created_at: string;
  provider: string;
  model: string;
  highlights?: {
    [field: string]: string[];
  };
}

export interface SummarySearchResponse {
  hits: SummarySearchHit[];
  total: number;
  max_score?: number;
  query: string;
  filters: Record<string, any>;
}

export interface SpeakerIdentificationResponse {
  message: string;
  task_id: string;
  file_id: string; // UUID
  speaker_count: number;
}
