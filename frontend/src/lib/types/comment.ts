/**
 * Comment on a media file, mirroring `backend/app/schemas/comment.py`.
 *
 * Import via `$lib/types/comment`. `CommentSection.svelte` is still plain JS and
 * describes the same shape in JSDoc; keep the two in step until it is converted.
 */
export interface CommentAuthor {
  uuid?: string;
  email?: string;
  full_name?: string;
  username?: string;
}

export interface Comment {
  uuid: string;
  text: string;
  /** Playback position the comment is anchored to, in seconds. */
  timestamp: number;
  created_at: string;
  /** Owning user's UUID; present even when `user` has not been expanded. */
  user_id?: string;
  /**
   * Expanded author. The backend omits it on some list responses, so the export
   * path fills it in from the auth store before rendering.
   */
  user?: CommentAuthor;
}
