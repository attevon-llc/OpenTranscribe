/**
 * Local Whisper model library (issue #1246): the catalog with what is on disk, plus the one
 * download. super_admin only; 409 `local_asr_unavailable` on lite. Mirrors
 * `backend/app/schemas/local_models.py`. Literal URLs on purpose (the route-has-a-caller scanner
 * reads them).
 */

import axiosInstance from '../axios';

export type DownloadStatus =
  | 'queued'
  | 'downloading'
  | 'verifying'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type LanguageSupport = 'multilingual' | 'english_only' | 'english_optimized';

export interface LocalModelDownloadState {
  model_id: string;
  status: DownloadStatus;
  bytes_done: number;
  bytes_total: number | null;
  percent: number | null;
  error_code: string | null;
  updated_at: string;
}

export interface LocalModelEntry {
  id: string;
  display_name: string;
  repo_id: string;
  approx_bytes: number;
  vram_hint_gb: number;
  language_support: LanguageSupport;
  supports_translation: boolean;
  is_default: boolean;
  downloaded: boolean;
  partial: boolean;
  present_bytes: number;
  is_active: boolean;
}

export interface LocalModelCatalog {
  models: LocalModelEntry[];
  download: LocalModelDownloadState | null;
  free_bytes: number;
  cache_writable: boolean;
  active_model: string;
}

/** The `local_model_download` WebSocket payload (no `updated_at`). */
export interface LocalModelDownloadEvent {
  model_id: string;
  status: DownloadStatus;
  bytes_done: number;
  bytes_total: number | null;
  percent: number | null;
  error_code: string | null;
}

export async function getLocalModelCatalog(): Promise<LocalModelCatalog> {
  const response = await axiosInstance.get('/admin/local-models');
  return response.data;
}

export async function startLocalModelDownload(modelId: string): Promise<LocalModelDownloadState> {
  const response = await axiosInstance.post('/admin/local-models/download', {
    model_id: modelId,
  });
  return response.data;
}

export async function cancelLocalModelDownload(): Promise<LocalModelDownloadState> {
  const response = await axiosInstance.post('/admin/local-models/download/cancel');
  return response.data;
}
