/** Conversation analytics computed by the backend for one transcribed file. */
export interface OverallAnalytics {
  word_count?: number;
  duration_seconds?: number;
  talk_time?: {
    by_speaker: Record<string, number>;
    total: number;
  };
  turn_taking?: {
    by_speaker: Record<string, number>;
    total_turns: number;
  };
  interruptions?: {
    by_speaker: Record<string, number>;
    total: number;
  };
  questions?: {
    by_speaker: Record<string, number>;
    total: number;
  };
}
