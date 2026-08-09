import axiosInstance from '../axios';

type ProtectedMediaField = {
  name: string;
  label: string;
  type: string;
};

export type ProtectedMediaAuthConfig = {
  hosts: string[];
  hosts_with_stored_credentials?: string[];
  auth_type: string;
  fields: ProtectedMediaField[];
};

let protectedConfigs: ProtectedMediaAuthConfig[] = [];
let loaded = false;
let loadingPromise: Promise<void> | null = null;

export async function loadProtectedMediaAuthConfig(): Promise<void> {
  if (loaded) return;
  if (loadingPromise) return loadingPromise;

  loadingPromise = (async () => {
    try {
      const resp = await axiosInstance.get<ProtectedMediaAuthConfig[]>(
        '/system/config/protected-media-auth'
      );
      protectedConfigs = resp.data ?? [];
      loaded = true;
    } catch (e) {
      // Config is optional; swallow errors and leave configs empty
      console.error('Failed to load protected media auth config', e);
      protectedConfigs = [];
      loaded = true;
    } finally {
      loadingPromise = null;
    }
  })();

  return loadingPromise;
}

export function getAuthConfigForHost(hostname: string): ProtectedMediaAuthConfig | null {
  for (const cfg of protectedConfigs) {
    if (cfg.hosts?.includes(hostname)) {
      return cfg;
    }
  }
  return null;
}
