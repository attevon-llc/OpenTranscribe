/**
 * Client for a speaker's identity history (issue #1336): the recorded changes to its name,
 * profile link and suggestion, newest first. Read-only.
 */
import axiosInstance from '$lib/axios';
import type { SpeakerIdentityEvent } from '$lib/types/speaker';

/** The backend's default page; it accepts 1-200. */
export const IDENTITY_HISTORY_LIMIT = 50;

export async function getSpeakerIdentityHistory(
  speakerUuid: string,
  limit = IDENTITY_HISTORY_LIMIT
): Promise<SpeakerIdentityEvent[]> {
  const response = await axiosInstance.get(
    `/speakers/${encodeURIComponent(speakerUuid)}/identity-history`,
    { params: { limit } }
  );
  return response.data;
}
