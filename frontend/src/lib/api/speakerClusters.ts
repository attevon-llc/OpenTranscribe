/**
 * API client for speaker clustering operations
 */
import axiosInstance from '../axios';
import type {
  SpeakerCluster,
  SpeakerClusterDetail,
  SpeakerInboxItem,
  PaginatedResponse,
  BatchVerifyResponse,
  ReclusterResponse,
  OutlierAnalysisResponse,
  SpeakerProfile,
} from '$lib/types/speakerCluster';

export async function listClusters(
  page = 1,
  perPage = 20,
  search?: string,
  hasLabel?: boolean
): Promise<PaginatedResponse<SpeakerCluster>> {
  const params: Record<string, string | number | boolean> = { page, per_page: perPage };
  if (search) params.search = search;
  if (hasLabel !== undefined) params.has_label = hasLabel;
  const response = await axiosInstance.get('/speaker-clusters', { params });
  return response.data;
}

export async function getClusterDetail(uuid: string): Promise<SpeakerClusterDetail> {
  const response = await axiosInstance.get(`/speaker-clusters/${uuid}`);
  return response.data;
}

export async function updateCluster(
  uuid: string,
  data: { label?: string; description?: string }
): Promise<SpeakerCluster> {
  const response = await axiosInstance.put(`/speaker-clusters/${uuid}`, data);
  return response.data;
}

export async function deleteCluster(uuid: string): Promise<void> {
  await axiosInstance.delete(`/speaker-clusters/${uuid}`);
}

export async function mergeClusters(
  sourceUuid: string,
  targetUuid: string
): Promise<SpeakerCluster> {
  const response = await axiosInstance.post(`/speaker-clusters/${sourceUuid}/merge/${targetUuid}`);
  return response.data;
}

export async function splitCluster(uuid: string, speakerUuids: string[]): Promise<SpeakerCluster> {
  const response = await axiosInstance.post(`/speaker-clusters/${uuid}/split`, {
    speaker_uuids: speakerUuids,
  });
  return response.data;
}

export async function promoteCluster(
  uuid: string,
  name: string,
  description?: string
): Promise<{ profile_uuid: string; profile_name: string; message: string }> {
  const response = await axiosInstance.post(`/speaker-clusters/${uuid}/promote`, {
    name,
    description,
  });
  return response.data;
}

export async function triggerRecluster(
  force = false,
  threshold?: number
): Promise<ReclusterResponse> {
  const response = await axiosInstance.post('/speaker-clusters/recluster', {
    force,
    threshold,
  });
  return response.data;
}

export async function getUnverifiedSpeakers(
  page = 1,
  perPage = 20
): Promise<PaginatedResponse<SpeakerInboxItem>> {
  const response = await axiosInstance.get('/speaker-clusters/unverified/inbox', {
    params: { page, per_page: perPage },
  });
  return response.data;
}

export async function batchVerifySpeakers(
  speakerUuids: string[],
  action: 'accept' | 'assign' | 'name' | 'skip',
  profileUuid?: string,
  displayName?: string
): Promise<BatchVerifyResponse> {
  const response = await axiosInstance.post('/speaker-clusters/batch-verify', {
    speaker_uuids: speakerUuids,
    action,
    profile_uuid: profileUuid,
    display_name: displayName,
  });
  return response.data;
}

export interface SpeakerMediaPreviewData {
  speaker_uuid: string;
  speaker_name: string;
  file_uuid: string;
  file_name: string;
  title: string;
  content_type: string;
  start_time: number;
  end_time: number;
  media_url: string | null;
}

export async function getSpeakerMediaPreview(
  speakerUuid: string
): Promise<SpeakerMediaPreviewData> {
  const response = await axiosInstance.get(
    `/speaker-clusters/speakers/${speakerUuid}/media-preview`
  );
  return response.data;
}

export async function updateProfile(
  uuid: string,
  data: { name?: string; description?: string }
): Promise<SpeakerProfile> {
  const params = new URLSearchParams();
  if (data.name) params.set('name', data.name);
  if (data.description !== undefined) params.set('description', data.description);
  const response = await axiosInstance.put(
    `/speaker-profiles/profiles/${uuid}?${params.toString()}`
  );
  return response.data;
}

export async function deleteProfile(uuid: string): Promise<void> {
  await axiosInstance.delete(`/speaker-profiles/profiles/${uuid}`);
}

/**
 * Merge a source speaker into a target speaker
 * @param sourceUuid - UUID of the speaker to merge (will be deleted)
 * @param targetUuid - UUID of the speaker to keep (will receive all segments)
 * @returns Updated target speaker
 */
export async function mergeSpeakers(
  sourceUuid: string,
  targetUuid: string
): Promise<import('$lib/types/speaker').MergeSpeakersResponse> {
  const response = await axiosInstance.post(`/speakers/${sourceUuid}/merge/${targetUuid}`);
  return response.data;
}

export async function listProfiles(): Promise<
  import('$lib/types/speakerCluster').SpeakerProfile[]
> {
  const response = await axiosInstance.get('/speaker-profiles/profiles');
  return response.data;
}

export async function uploadProfileAvatar(
  profileUuid: string,
  file: File
): Promise<{ uuid: string; avatar_url: string }> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await axiosInstance.post(
    `/speaker-profiles/profiles/${profileUuid}/avatar`,
    formData,
    { headers: { 'Content-Type': 'multipart/form-data' } }
  );
  return response.data;
}

export async function deleteProfileAvatar(profileUuid: string): Promise<void> {
  await axiosInstance.delete(`/speaker-profiles/profiles/${profileUuid}/avatar`);
}

export async function confirmSpeakerGender(
  speakerUuid: string,
  gender: string
): Promise<{ speaker_uuid: string; predicted_gender: string; gender_confirmed_by_user: boolean }> {
  const response = await axiosInstance.post(
    `/speakers/${speakerUuid}/confirm-gender?gender=${encodeURIComponent(gender)}`
  );
  return response.data;
}

export async function analyzeClusterOutliers(
  clusterUuid: string
): Promise<OutlierAnalysisResponse> {
  const response = await axiosInstance.post(`/speaker-clusters/${clusterUuid}/analyze-outliers`);
  return response.data;
}

export async function unassignSpeakers(
  clusterUuid: string,
  speakerUuids: string[],
  blacklist = true
): Promise<{ unassigned_count: number; message: string }> {
  const response = await axiosInstance.post(`/speaker-clusters/${clusterUuid}/unassign`, {
    speaker_uuids: speakerUuids,
    blacklist,
  });
  return response.data;
}

export async function confirmProfileGender(
  profileUuid: string,
  gender: string
): Promise<{ profile_uuid: string; predicted_gender: string; updated_count: number }> {
  const response = await axiosInstance.post(
    `/speaker-profiles/profiles/${profileUuid}/confirm-gender?gender=${encodeURIComponent(gender)}`
  );
  return response.data;
}
