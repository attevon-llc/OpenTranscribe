import type { ProcessingStep } from '$lib/types/media';

/** `GET /my-files/status` (backend `api/endpoints/user_files.py::get_user_file_status`). */
export interface UserFileStatusSummary {
  status_counts: {
    total: number;
    completed: number;
    processing: number;
    pending: number;
    error: number;
  };
  problem_files: { count: number };
  has_problems: boolean;
}

/** One entry of `task_details` from `GET /my-files/{uuid}/status`. */
export interface TaskDetail {
  id: string;
  task_type: string;
  status: string;
  progress?: number | null;
  created_at: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  error_message?: string | null;
  status_badge_class?: string;
  formatted_processing_time?: string;
  whisper_model?: string | null;
  diarization_model?: string | null;
}

/** One row of `GET /tasks`: a task plus the media file it belongs to. */
export interface UserTask {
  id: string;
  task_type: string;
  status: string;
  progress?: number | null;
  error_message?: string | null;
  media_file?: { uuid: string; filename: string } | null;
}

/** `GET /my-files/{uuid}/status`. */
export interface DetailedFileStatus {
  file: {
    uuid: string;
    filename: string;
    status: string;
    display_status?: string;
    status_badge_class?: string;
    upload_time: string | null;
    completed_at?: string | null;
    language?: string | null;
    formatted_file_size?: string;
    formatted_duration?: string;
    formatted_file_age?: string;
    whisper_model?: string | null;
    requested_whisper_model?: string | null;
    model_fallback_occurred?: boolean;
    diarization_model?: string | null;
    diarization_disabled?: boolean;
    processing_steps?: ProcessingStep[];
  };
  task_details: TaskDetail[];
  can_retry: boolean;
  is_stuck: boolean;
}
