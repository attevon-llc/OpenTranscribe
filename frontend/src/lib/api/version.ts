import axiosInstance from '$lib/axios';

export interface ServerVersion {
  version: string;
  git_sha: string;
}

/**
 * The running server's build identity (`GET /api/version`). A strict superset of `/health`:
 * the same `version`, plus the `git_sha` the About dialog's AGPL source offer must resolve to.
 * Missing fields come back as `''` so callers can treat "unknown" uniformly.
 */
export async function getServerVersion(): Promise<ServerVersion> {
  const { data } = await axiosInstance.get('/version');
  return { version: data?.version || '', git_sha: data?.git_sha || '' };
}
