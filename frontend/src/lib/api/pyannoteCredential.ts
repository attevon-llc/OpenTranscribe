/**
 * The current user's pyannote.ai diarization API key (issue #1204).
 *
 * Mirrors `backend/app/schemas/diarization_settings.py`. The key is write-only: it is sent
 * by `savePyannoteCredential` / `testPyannoteCredential` and returned by nothing, so a form
 * must never pre-fill it. The routes 404 when the deployment turns off the
 * `asr.user_providers` capability.
 */

import axiosInstance from '../axios';

const BASE = '/user-settings/diarization/pyannote';

export interface PyannoteCredentialStatus {
  configured: boolean;
  /** The deployment manages the speaker-detection source; writes answer 409. */
  locked: boolean;
  test_status: 'success' | 'failed' | null;
  test_message: string | null;
  last_tested: string | null;
  updated_at: string | null;
}

export interface PyannoteCredentialDeleted {
  deleted: boolean;
  /** The user's speaker-detection source after the delete. */
  diarization_source: string;
  /** The source was `pyannote` and has been reset to the default. */
  source_reverted: boolean;
}

export type PyannoteTestCode = 'connected' | 'rejected' | 'unreachable' | 'error';

export interface PyannoteTestResult {
  success: boolean;
  code: PyannoteTestCode;
  /** Fixed English sentence from the server; prefer translated copy keyed by `code`. */
  message: string;
  response_time_ms: number;
}

export async function getPyannoteCredential(): Promise<PyannoteCredentialStatus> {
  const response = await axiosInstance.get(BASE);
  return response.data;
}

export async function savePyannoteCredential(apiKey: string): Promise<PyannoteCredentialStatus> {
  const response = await axiosInstance.put(BASE, { api_key: apiKey });
  return response.data;
}

export async function deletePyannoteCredential(): Promise<PyannoteCredentialDeleted> {
  const response = await axiosInstance.delete(BASE);
  return response.data;
}

/**
 * Check a key against pyannote.ai's free `GET /v1/test`; no audio is sent and no job runs.
 * With `apiKey` that key is tested and nothing is stored; without it the saved key is.
 */
export async function testPyannoteCredential(apiKey?: string): Promise<PyannoteTestResult> {
  const body = apiKey === undefined ? {} : { api_key: apiKey };
  const response = await axiosInstance.post(`${BASE}/test`, body);
  return response.data;
}
