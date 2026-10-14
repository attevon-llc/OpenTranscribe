/**
 * The file-detail player's caption track, as WebVTT, from the SERVER.
 *
 * `GET /api/files/{uuid}/captions` cuts the transcript into broadcast-style cues (at most
 * two short lines, timed from word timestamps, the speaker's name on each new turn) with
 * the same segmenter as the SRT/WebVTT downloads, under the reader's redaction policy.
 * The player used to build this track in the browser, one cue per diarized segment, so a
 * minute of speech covered the video at once.
 *
 * ⚠️ Never add a local fallback that builds cues from `file.transcript_segments`: a refused
 * track (409 while the redaction scan runs, 503 when the policy cannot be read) must stay
 * empty, exactly like the transcript beside the player.
 */
import axiosInstance from '$lib/axios';

export interface CaptionTrackRequest {
  fileUuid: string;
  /** The owner/admin has revealed the original on the page (`?redact=false`). */
  showOriginal?: boolean;
  /** How to show an unnamed diarization speaker, with a `{number}` token. */
  speakerPlaceholder?: string;
}

export async function fetchCaptionTrack(request: CaptionTrackRequest): Promise<string> {
  const params: Record<string, string | boolean> = {};
  if (request.showOriginal) params.redact = false;
  if (request.speakerPlaceholder) params.speaker_placeholder = request.speakerPlaceholder;
  const response = await axiosInstance.get(`/files/${request.fileUuid}/captions`, {
    params,
    responseType: 'text',
    // The body is WebVTT, not JSON: keep axios from trying to parse it.
    transformResponse: (data: unknown) => data,
  });
  return typeof response.data === 'string' ? response.data : '';
}
