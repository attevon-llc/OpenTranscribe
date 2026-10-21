/**
 * The platform's Hugging Face access token (super_admin only).
 *
 * Mirrors `backend/app/api/endpoints/admin_hf_token.py`. The token is write-only: it is sent by
 * `saveHfToken` / `testHfToken` and returned by nothing, so a form must never pre-fill it. The
 * status says only whether one is in effect and where it comes from.
 */

import axiosInstance from '../axios';

const BASE = '/admin/hf-token';

export type HfTokenSource = 'app' | 'environment' | 'none';

export interface HfTokenStatus {
  is_set: boolean;
  source: HfTokenSource;
}

export type HfTokenTestCode = 'valid' | 'invalid' | 'unreachable';

export interface HfTokenTestResult {
  ok: boolean;
  code: HfTokenTestCode;
  /** Fixed English sentence from the server; prefer translated copy keyed by `code`. */
  message: string;
}

export async function getHfTokenStatus(): Promise<HfTokenStatus> {
  const response = await axiosInstance.get(BASE);
  return response.data;
}

export async function saveHfToken(token: string): Promise<HfTokenStatus> {
  const response = await axiosInstance.put(BASE, { token });
  return response.data;
}

export async function removeHfToken(): Promise<HfTokenStatus> {
  const response = await axiosInstance.delete(BASE);
  return response.data;
}

/**
 * Ask Hugging Face whether a token is accepted. With `token` that one is tested and nothing is
 * stored; without it the token currently in effect is.
 */
export async function testHfToken(token?: string): Promise<HfTokenTestResult> {
  const response = await axiosInstance.post(`${BASE}/test`, token === undefined ? {} : { token });
  return response.data;
}
