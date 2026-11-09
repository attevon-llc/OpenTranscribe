import axios from 'axios';
import { get } from 'svelte/store';
import { t } from '$stores/locale';

/** Duration as `2m 30s`; empty while there is nothing meaningful to show yet. */
export function formatTimeRemaining(ms: number): string {
  if (!ms || ms <= 0) return '';

  const seconds = Math.ceil(ms / 1000);
  if (seconds < 60) return `${seconds}s`;

  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;

  if (minutes < 60) {
    return remainingSeconds > 0 ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${remainingMinutes}m`;
}

export function getSourceName(source: File | string | Blob): string {
  if (source instanceof File) {
    return source.name;
  } else if (typeof source === 'string') {
    try {
      const url = new URL(source);
      // Try to extract YouTube video title or use a more descriptive name
      if (url.hostname.includes('youtube.com') || url.hostname.includes('youtu.be')) {
        return get(t)('upload.youtubeVideo');
      }
      return url.pathname.split('/').pop() || get(t)('upload.videoUrl');
    } catch {
      return get(t)('upload.videoUrl');
    }
  } else {
    // Blob (recording)
    return `recording_${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;
  }
}

export function getErrorMessage(error: unknown): string {
  if (axios.isCancel(error)) {
    return get(t)('upload.cancelled');
  }

  const e = error as { response?: { data?: { detail?: string } }; message?: string };
  if (e?.response?.data?.detail) {
    return e.response.data.detail;
  }

  if (e?.message) {
    return e.message;
  }

  return get(t)('common.unknownError');
}
