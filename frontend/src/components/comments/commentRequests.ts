/**
 * The comment requests for one file. Each tries the current endpoint first and, only when the
 * server answers 404 (an older backend), the two legacy shapes in turn; any other failure is
 * the caller's to report as it is.
 */
import type { AxiosInstance, AxiosResponse } from 'axios';

/** Every endpoint answered with an error: `cause` is the last one's. */
export class CommentEndpointsExhausted extends Error {
  constructor(public readonly cause: unknown) {
    super('All comment endpoints failed');
    this.name = 'CommentEndpointsExhausted';
  }
}

export interface RequestErrorLike {
  message?: string;
  response?: { status?: number; data?: unknown };
}

/**
 * Runs `primary`; on a 404 only, runs each fallback in order until one succeeds. Throws the
 * primary's error unchanged when it was not a 404, else `CommentEndpointsExhausted` carrying the
 * last fallback's error. `onPrimaryError` sees every primary failure first (for logging).
 */
export async function withLegacyFallbacks<T>(
  primary: () => Promise<T>,
  fallbacks: Array<() => Promise<T>>,
  onPrimaryError?: (error: RequestErrorLike) => void
): Promise<T> {
  try {
    return await primary();
  } catch (error) {
    onPrimaryError?.(error as RequestErrorLike);
    if ((error as RequestErrorLike)?.response?.status !== 404) throw error;
    let last: unknown = error;
    for (const attempt of fallbacks) {
      try {
        return await attempt();
      } catch (fallbackError) {
        last = fallbackError;
      }
    }
    throw new CommentEndpointsExhausted(last);
  }
}

export function fetchFileComments(
  client: AxiosInstance,
  fileId: string,
  onPrimaryError?: (error: RequestErrorLike) => void
): Promise<AxiosResponse> {
  return withLegacyFallbacks(
    () => client.get(`/comments/files/${fileId}/comments`),
    [
      () => client.get(`/files/${fileId}/comments`),
      () => client.get('/comments', { params: { media_file_id: fileId } }),
    ],
    onPrimaryError
  );
}

export function postFileComment(
  client: AxiosInstance,
  fileId: string,
  text: string,
  timestamp: number | null,
  onPrimaryError?: (error: RequestErrorLike) => void
): Promise<AxiosResponse> {
  // The backend expects media_file_id in the payload even though it is in the URL path.
  const payload = { text, timestamp, media_file_id: fileId };
  return withLegacyFallbacks(
    () => client.post(`/comments/files/${fileId}/comments`, payload),
    [
      () => client.post(`/files/${fileId}/comments`, payload),
      () => client.post('/comments', payload),
    ],
    onPrimaryError
  );
}

interface CommentUser {
  uuid?: string;
  email?: string;
  full_name?: string;
}

/**
 * The comment, with a user object built from the signed-in user when the server sent none.
 * `uuid` is what that fabricated user is identified by: the caller decides the fallback.
 */
export function withUser<C extends { user?: unknown; user_id?: string }>(
  comment: C,
  currentUser: CommentUser | null | undefined,
  uuid: string | undefined
): C & { user: unknown } {
  if (comment.user) return comment as C & { user: unknown };
  return {
    ...comment,
    user: {
      uuid,
      email: currentUser?.email,
      full_name: currentUser?.full_name || currentUser?.email || 'User ' + comment.user_id,
    },
  };
}
