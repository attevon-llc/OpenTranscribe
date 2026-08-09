import axiosInstance from '../axios';

export interface MediaSource {
  uuid: string;
  hostname: string;
  provider_type: string;
  username: string;
  has_credentials: boolean;
  verify_ssl: boolean;
  label: string;
  is_active: boolean;
  is_shared: boolean;
  shared_at?: string;
  owner_name?: string;
  owner_role?: string;
  is_own: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface MediaSourceCreate {
  hostname: string;
  provider_type?: string;
  username?: string;
  password?: string;
  verify_ssl?: boolean;
  label?: string;
}

export interface MediaSourceUpdate {
  hostname?: string;
  provider_type?: string;
  username?: string;
  password?: string;
  verify_ssl?: boolean;
  label?: string;
  is_shared?: boolean;
}

export interface MediaSourcesResponse {
  sources: MediaSource[];
  shared_sources: MediaSource[];
}

/**
 * Get user's own media sources and shared sources from other users.
 */
export async function getMediaSources(): Promise<MediaSourcesResponse> {
  const resp = await axiosInstance.get<MediaSourcesResponse>('/user-settings/media-sources');
  return resp.data;
}

export async function addMediaSource(source: MediaSourceCreate): Promise<MediaSource> {
  const resp = await axiosInstance.post<MediaSource>('/user-settings/media-sources', source);
  return resp.data;
}

export async function updateMediaSource(
  uuid: string,
  update: MediaSourceUpdate
): Promise<MediaSource> {
  const resp = await axiosInstance.put<MediaSource>(`/user-settings/media-sources/${uuid}`, update);
  return resp.data;
}

export async function deleteMediaSource(uuid: string): Promise<void> {
  await axiosInstance.delete(`/user-settings/media-sources/${uuid}`);
}

export async function toggleMediaSourceShare(
  uuid: string,
  isShared: boolean
): Promise<MediaSource> {
  const resp = await axiosInstance.put<MediaSource>(`/user-settings/media-sources/${uuid}`, {
    is_shared: isShared,
  });
  return resp.data;
}
