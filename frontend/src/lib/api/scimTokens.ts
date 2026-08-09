/**
 * API client for SCIM 2.0 provisioning tokens (`/api/admin/scim-tokens`, super_admin).
 *
 * Mirrors `backend/app/api/endpoints/admin_scim_tokens.py` field-for-field. The
 * plaintext token is returned once, by `create`, and never again -- the row stores
 * only a SHA-256 digest.
 */
import axiosInstance from '../axios';

export interface ScimToken {
  uuid: string;
  name: string;
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ScimTokenCreateRequest {
  name: string;
  /** Omitted means "until revoked". */
  expires_at?: string | null;
}

export interface ScimTokenCreatedResponse extends ScimToken {
  token: string;
}

export class ScimTokensApi {
  static async list(): Promise<ScimToken[]> {
    const response = await axiosInstance.get('/admin/scim-tokens');
    return response.data;
  }

  static async create(payload: ScimTokenCreateRequest): Promise<ScimTokenCreatedResponse> {
    const response = await axiosInstance.post('/admin/scim-tokens', payload);
    return response.data;
  }

  static async revoke(uuid: string): Promise<ScimToken> {
    const response = await axiosInstance.delete(`/admin/scim-tokens/${uuid}`);
    return response.data;
  }
}
