import axiosInstance from '$lib/axios';

/** `GET /auth/demo`: reset and upgrade times, or `enabled: false` outside a public demo. */
export interface DemoInfo {
  enabled: boolean;
  next_reset_at: string | null;
  upgraded_at: string | null;
}

/** Licence and credit the seeder recorded for one demo file. */
export interface DemoAttribution {
  file_uuid: string;
  license: string;
  license_url: string;
  attribution: string;
  source_url: string;
}

export async function getDemoInfo(): Promise<DemoInfo> {
  const { data } = await axiosInstance.get<DemoInfo>('/auth/demo');
  return {
    enabled: data?.enabled === true,
    next_reset_at: data?.next_reset_at ?? null,
    upgraded_at: data?.upgraded_at ?? null,
  };
}

export async function getDemoAttributions(): Promise<DemoAttribution[]> {
  const { data } = await axiosInstance.get<{ items: DemoAttribution[] }>('/auth/demo/attributions');
  return data?.items ?? [];
}

/** `POST /auth/demo/session`: sign in as the shared demo user, no password. */
export async function postDemoSession(): Promise<{ access_token?: string }> {
  const { data } = await axiosInstance.post<{ access_token?: string }>('/auth/demo/session');
  return data ?? {};
}
