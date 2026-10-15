/**
 * Cloud ASR provider policy (issue #1246): which cloud transcription providers plain users may
 * choose, and whether they may save keys of their own. `/admin/asr-providers` is super_admin
 * only. Credentials are write-only: the server reports only whether each shared config holds
 * a key (`has_*`), never the key.
 */

import axiosInstance from '../axios';

export interface SharedCredential {
  config_uuid: string;
  name: string;
  has_api_key: boolean;
  has_access_key_id: boolean;
  owner_name: string | null;
}

export interface PolicyProvider {
  id: string;
  display_name: string;
  allowed_for_users: boolean;
  shared_credentials: SharedCredential[];
}

export interface ProviderPolicy {
  providers: PolicyProvider[];
  users_may_add_own_keys: boolean;
}

export interface ProviderPolicyUpdate {
  /** `null` means every provider is allowed (including ones added later). */
  allowed_for_users: string[] | null;
  users_may_add_own_keys: boolean;
}

export async function getProviderPolicy(): Promise<ProviderPolicy> {
  const response = await axiosInstance.get('/admin/asr-providers');
  return response.data;
}

export async function updateProviderPolicy(body: ProviderPolicyUpdate): Promise<ProviderPolicy> {
  const response = await axiosInstance.put('/admin/asr-providers', body);
  return response.data;
}

/** The PUT body for what the toggles show: all allowed is `null`, never a list of every id. */
export function buildPolicyUpdate(
  providers: PolicyProvider[],
  usersMayAddOwnKeys: boolean
): ProviderPolicyUpdate {
  const allowed = providers.filter((p) => p.allowed_for_users).map((p) => p.id);
  return {
    allowed_for_users: allowed.length === providers.length ? null : allowed,
    users_may_add_own_keys: usersMayAddOwnKeys,
  };
}
