<script>
  import { onMount, createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  // Use the shared axios instance so auth token is always sent
  import axiosInstance from '../lib/axios';
  import { authStore } from '../stores/auth';
  import TruncatedText from './TruncatedText.svelte';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import { toastStore } from '../stores/toast';
  import { t } from '../stores/locale';

  // The Svelte component is exported by default automatically

  /** @type {string} */
  export let fileId = "";
  /** @type {number} */
  export let currentTime = 0;
  /**
   * v400 (#362 lane C5) — notes on a document, the document analogue of a media
   * comment. When `mode === 'document'` this component talks to
   * `/comments/documents/{documentId}/comments` instead of the media nested route,
   * and hides the timestamp/"mark current time" affordance (a document has no
   * playback axis). `documentId` is the document's public uuid.
   * @type {'media' | 'document'}
   */
  export let mode = 'media';
  /** @type {string} */
  export let documentId = "";

  $: isDocument = mode === 'document';
  $: targetId = isDocument ? documentId : fileId;
  $: commentsEndpoint = isDocument
    ? `/comments/documents/${targetId}/comments`
    : `/comments/files/${targetId}/comments`;

  /** @type {Array<{uuid: string, text: string, timestamp: number | null, document_chunk_index?: number | null, user: {uuid: string, email?: string, full_name?: string, username?: string}, created_at: string}>} */
  let comments = [];
  /** @type {boolean} */
  let loading = true;
  /** @type {string} */
  let newComment = '';
  /** @type {number|null} */
  let timestampInput = null;
  /** @type {string|null} */
  let editingCommentId = null;
  /** @type {string} */
  let editingCommentText = '';

  // Confirmation modal state
  let showConfirmModal = false;
  let confirmModalTitle = '';
  let confirmModalMessage = '';
  /** @type {(() => void) | null} */
  let confirmCallback = null;

  // Event dispatcher
  const dispatch = createEventDispatcher();

  // Tell the page how many comments exist, so the export flow doesn't have to refetch them
  // just to decide whether to offer the "include comments" option.
  //
  // This replaces a `getComments` DOM-event bridge that listened on
  // `.comments-section-wrapper` — an element that exists nowhere in the app, so the data
  // was never actually reachable and the export path fetched it again.
  $: dispatch('commentsChanged', { count: comments.length });

  onMount(() => {
    fetchComments();
  });

  async function fetchComments() {
    loading = true;
    try {
      // Validate the target id
      if (!targetId) {
        console.error('Invalid target ID for comments:', mode, targetId);
        toastStore.error($t('comments.invalidFileId'));
        loading = false;
        return;
      }

      // Document mode has no legacy history to fall back to — the nested route
      // is the only route it has ever had.
      if (isDocument) {
        try {
          const response = await axiosInstance.get(commentsEndpoint);
          comments = Array.isArray(response.data) ? response.data : [];
          comments.sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0));
        } catch (/** @type {any} */ error) {
          if (error.response?.status === 401) {
            toastStore.error($t('comments.loginRequired'));
          } else {
            toastStore.error($t('comments.loadFailed', { error: error.message }));
          }
        } finally {
          loading = false;
        }
        return;
      }

      let response;
      try {
        const endpoint = commentsEndpoint;
        response = await axiosInstance.get(endpoint);
      } catch (/** @type {any} */ error) {
        console.error('Error fetching comments:', error?.message, error?.response?.status, error?.response?.data);

        // If the error is a 401, this is an authentication issue
        if (error.response?.status === 401) {
          toastStore.error($t('comments.loginRequired'));
          loading = false;
          return;
        }

        // Try alternate endpoints as fallback
        if (error.response?.status === 404) {
          try {
            // Try the legacy endpoint as fallback
            response = await axiosInstance.get(`/files/${fileId}/comments`);
          } catch (/** @type {any} */ legacyError) {
            try {
              // If that fails, try the query parameter approach as last resort
              response = await axiosInstance.get('/comments', {
                params: { media_file_id: fileId }
              });
            } catch (/** @type {any} */ lastError) {
              console.error('[CommentSection] All endpoints failed');
              toastStore.error($t('comments.loadFailed', { error: lastError.message }));
              return;
            }
          }
        } else {
          toastStore.error($t('comments.loadFailed', { error: error.message }));
          return;
        }
      }

      // Comments response received

      // Process comments to ensure they have user information
      // Processing comments
      comments = response.data.map(/** @param {any} comment */ (comment) => {
        // If comment has no user info, add it from the auth store
        if (!comment.user) {
          const currentUser = $authStore.user;
          return {
            ...comment,
            user: {
              uuid: comment.user_id,
              email: currentUser?.email,
              full_name: currentUser?.full_name || currentUser?.email || 'User ' + comment.user_id
            }
          };
        }
        return comment;
      });

      if (!Array.isArray(comments) || comments.length === 0) {
        // No comments found for this file
      } else {
        // Successfully processed comments
      }

      // Sort by timestamp
      comments.sort((/** @type {{timestamp: number}} */ a, /** @type {{timestamp: number}} */ b) => a.timestamp - b.timestamp);
    } catch (/** @type {any} */ err) { // Handle error with proper type
      console.error('[CommentSection] Error fetching comments:', err);
      console.error('[CommentSection] Error details:', {
        message: err.message,
        response: err.response,
        status: err.response?.status,
        data: err.response?.data
      });

      // Provide a more user-friendly error message
      if (err.response && err.response.status === 401) {
        toastStore.error($t('comments.loginRequired'));
      } else if (err.code === 'ERR_NETWORK') {
        toastStore.error($t('comments.networkError'));
      } else {
        toastStore.error($t('comments.loadFailed', { error: err.message }));
      }
    } finally {
      loading = false;
    }
  }

  // Add a new comment
  /** @param {Event=} event - Optional event parameter */
  async function addComment(event) {
    // Always prevent default form submission behavior
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }

    if (!newComment.trim()) return;

    // Use the timestamp input if it was explicitly set, otherwise use null
    const timestamp = timestampInput !== null ? timestampInput : null;

    if (isDocument) {
      const currentUser = $authStore.user;
      try {
        const response = await axiosInstance.post(commentsEndpoint, { text: newComment });
        const commentWithUser = {
          ...response.data,
          user: response.data.user || {
            uuid: response.data.user_id || currentUser?.uuid || '',
            email: currentUser?.email,
            full_name: currentUser?.full_name || currentUser?.email || 'User ' + response.data.user_id
          }
        };
        comments = [...comments, commentWithUser];
        newComment = '';
        dispatch('commentAdded', commentWithUser);
      } catch (/** @type {any} */ err) {
        if (err.response?.status === 401) {
          toastStore.error($t('comments.loginRequiredToAdd'));
        } else {
          toastStore.error($t('comments.addFailed', { error: err.message }));
        }
      }
      return;
    }

    // Store locally for optimistic UI updates
    try {
      // Get user data from auth store
      const currentUser = $authStore.user;

      // Use the endpoint confirmed to be working in the API debugger
      let response;
      try {
        // The correct endpoint with leading slash (baseURL is '/api')
        // This will become /api/comments/files/{fileId}/comments
        const endpoint = `/comments/files/${fileId}/comments`;

        // The backend expects media_file_id in the payload even though it's in the URL path
        const commentPayload = {
          text: newComment,
          timestamp: timestamp,
          media_file_id: fileId // Required by the CommentCreate schema
        };

        // Cookies handle authentication automatically
        response = await axiosInstance.post(endpoint, commentPayload);

        // Successfully added comment
      } catch (/** @type {any} */ error) {
        // Log detailed error information for debugging
        console.error('[CommentSection] Error adding comment:', error);
        console.error('[CommentSection] Error response:',
          error.response?.status,
          error.response?.data
        );

        // If the error is a 401, this is an authentication issue
        if (error.response?.status === 401) {
          toastStore.error($t('comments.loginRequiredToAdd'));
          return;
        }

        // Try alternate endpoints as fallback if we get a 404
        if (error.response?.status === 404) {
          try {
            // Try the legacy endpoint approach as fallback
            // Trying legacy endpoint for adding comment
            const legacyPayload = {
              text: newComment,
              timestamp: timestamp,
              media_file_id: fileId // Required by the CommentCreate schema
            };
            response = await axiosInstance.post(`/files/${fileId}/comments`, legacyPayload);
            // Successfully added comment using main endpoint
          } catch (/** @type {any} */ legacyError) {
            try {
              // Try the query parameter endpoint as last resort
              // Trying query parameter endpoint as last resort
              response = await axiosInstance.post('/comments', {
                text: newComment,
                timestamp,
                media_file_id: fileId // Required by the CommentCreate schema
              });
              // Successfully added comment using root endpoint
            } catch (/** @type {any} */ lastError) {
              console.error('[CommentSection] All endpoints failed for adding comment');
              toastStore.error($t('comments.addFailed', { error: lastError.message }));
              return;
            }
          }
        } else {
          toastStore.error($t('comments.addFailed', { error: error.message }));
          return;
        }
      }

      // Comment added successfully

      // Ensure the comment has a user object with username
      const commentWithUser = {
        ...response.data,
        // Add user info from auth store if not present in the response
        user: response.data.user || {
          uuid: response.data.user_id || currentUser?.uuid || '',
          email: currentUser?.email,
          full_name: currentUser?.full_name || currentUser?.email || 'User ' + response.data.user_id
        }
      };

      comments = [...comments, commentWithUser];
      comments.sort((a, b) => a.timestamp - b.timestamp);
      newComment = '';
      timestampInput = null;
      dispatch('commentAdded', commentWithUser);
    } catch (/** @type {any} */ err) { // Handle error with proper type
      console.error('[CommentSection] Error adding comment:', err);
      console.error('[CommentSection] Error details:', {
        message: err.message,
        response: err.response,
        status: err.response?.status,
        data: err.response?.data
      });

      // Provide a more user-friendly error message
      if (err.response && err.response.status === 401) {
        toastStore.error($t('comments.loginRequiredToAdd'));
      } else if (err.code === 'ERR_NETWORK') {
        toastStore.error($t('comments.networkError'));
      } else {
        toastStore.error($t('comments.addFailed', { error: err.message }));
      }
    }
  }

  // Edit a comment
  /**
   * Edit an existing comment
   * @param {string} commentId - The UUID of the comment to edit
   * @param {string} newText - The new text for the comment
   */
  async function editComment(commentId, newText) {
    if (!newText || !newText.trim()) {
      toastStore.error($t('comments.textCannotBeEmpty'));
      return;
    }

    try {
      // Editing comment

      // Use PUT as the backend expects PUT for updates
      await axiosInstance.put(`/comments/${commentId}`, {
        text: newText
      });

      // Comment edited successfully

      // Update the comment in the list
      comments = comments.map(c => c.uuid === commentId ? { ...c, text: newText } : c);

      // Reset editing state if we were editing this comment
      if (editingCommentId === commentId) {
        editingCommentId = null;
        editingCommentText = '';
      }

    } catch (/** @type {any} */ err) {
      console.error('[CommentSection] Error editing comment:', err);
      console.error('[CommentSection] Error details:', {
        message: err.message,
        response: err.response,
        status: err.response?.status,
        data: err.response?.data
      });

      // Provide a more user-friendly error message
      if (err.response && err.response.status === 401) {
        toastStore.error($t('comments.loginRequiredToEdit'));
      } else {
        toastStore.error($t('comments.editFailed', { error: err.message }));
      }
    }
  }

  /**
   * Show confirmation modal
   * @param {string} title - The modal title
   * @param {string} message - The confirmation message
   * @param {() => void} callback - The callback to execute on confirmation
   */
  function showConfirmation(title, message, callback) {
    confirmModalTitle = title;
    confirmModalMessage = message;
    confirmCallback = callback;
    showConfirmModal = true;
  }

  /**
   * Handle confirmation modal confirm
   */
  function handleConfirmModalConfirm() {
    if (confirmCallback) {
      confirmCallback();
      confirmCallback = null;
    }
    showConfirmModal = false;
  }

  /**
   * Handle confirmation modal cancel
   */
  function handleConfirmModalCancel() {
    confirmCallback = null;
    showConfirmModal = false;
  }

  /**
   * Delete a comment
   * @param {string} commentId - The UUID of the comment to delete
   */
  async function deleteComment(commentId) {
    showConfirmation(
      $t('comments.deleteTitle'),
      $t('comments.deleteConfirmMessage'),
      () => executeDeleteComment(commentId)
    );
  }

  /**
   * Execute comment deletion after confirmation
   * @param {string} commentId - The UUID of the comment to delete
   */
  async function executeDeleteComment(commentId) {
    try {
      // Delete the comment (cookies handle authentication)
      await axiosInstance.delete(`/comments/${commentId}`);
      // Comment deleted successfully

      comments = comments.filter(c => c.uuid !== commentId);
    } catch (/** @type {any} */ err) { // Handle error with proper type
      console.error('[CommentSection] Error deleting comment:', err);
      console.error('[CommentSection] Error details:', {
        message: err.message,
        response: err.response,
        status: err.response?.status,
        data: err.response?.data
      });

      // Provide a more user-friendly error message
      if (err.response && err.response.status === 401) {
        toastStore.error($t('comments.loginRequiredToDelete'));
      } else {
        toastStore.error($t('comments.deleteFailed', { error: err.message }));
      }
    }
  }

  // Format timestamp as MM:SS
  /**
   * Format timestamp as MM:SS
   * @param {number} seconds - The timestamp in seconds
   * @returns {string} Formatted timestamp
   */
  function formatTimestamp(seconds) {
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.floor(seconds % 60);
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
  }

  // Use the current playback time
  /** @param {Event} event */
  function useCurrentTime(event) {
    // Prevent default button behavior
    if (event && event.preventDefault) {
      event.preventDefault();
    }

    // Get the current time directly from the player
    // This ensures we get the exact current time from the player
    timestampInput = currentTime;
    // Set timestamp to current time
  }

  // Check if the current user is the author of a comment
  /**
   * Check if the current user is the author of a comment
   * @param {string} userId - The user UUID to check
   * @returns {boolean} Whether the current user is the author
   */
  function isCommentAuthor(userId) {
    if (!$authStore.user) return false;

    // UUIDs are strings, so compare directly
    return $authStore.user.uuid === userId;
  }
</script>

<!-- Main wrapper with fixed header and scrollable comments -->
<div class="comments-wrapper">
  <div class="comments-header-internal">
    <h4 class="section-heading">{$t('comments.title')}</h4>
    <div class="comments-preview">
      {#if comments && comments.length > 0}
        <span class="tag-chip">{comments.length} {comments.length !== 1 ? $t('comments.comments') : $t('comments.comment')}</span>
      {:else}
        <span class="no-comments">{$t('comments.noComments')}</span>
      {/if}
    </div>
  </div>

  <!-- Fixed comment form at the top -->
  <div class="comment-form-container">
    <form class="comment-form" on:submit={addComment}>
      <textarea
        bind:value={newComment}
        placeholder={$t('comments.placeholder')}
        rows="2"
        title={$t('comments.enterCommentHint')}
      ></textarea>
      <div class="form-actions">
        {#if !isDocument}
          <div class="timestamp-actions">
            {#if timestampInput === null}
              <button
                type="button"
                class="timestamp-button"
                on:click={useCurrentTime}
                title={$t('comments.markTimeHint')}
              >
                <span class="button-icon">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="10"/>
                    <polyline points="12,6 12,12 16,14"/>
                  </svg>
                </span>
                <span>{$t('comments.markCurrentTime')}</span>
              </button>
            {:else}
              <div class="current-timestamp">
                <span class="timestamp-value">{$t('comments.markedTime', { time: formatTimestamp(timestampInput) })}</span>
                <button
                  type="button"
                  class="clear-button"
                  on:click|stopPropagation={() => timestampInput = null}
                  title={$t('comments.clearTimestampHint')}
                >
                  ✖
                </button>
              </div>
            {/if}
          </div>
        {:else}
          <!-- Documents have no playback timeline, so there is nothing to mark — the
               spacer keeps the submit button right-aligned the same way the media
               form does. -->
          <div class="timestamp-actions"></div>
        {/if}
        <button
          type="submit"
          class="submit-button"
          disabled={!newComment.trim() || (!isDocument && timestampInput === null)}
          title={!newComment.trim()
            ? $t('comments.mustAddTextAndTime')
            : (!isDocument && timestampInput === null)
              ? $t('comments.mustAddTextAndTime')
              : isDocument
                ? $t('comments.addComment')
                : $t('comments.addCommentAtTime', { time: formatTimestamp(timestampInput) })}
        >
          {$t('comments.addComment')}
        </button>
      </div>
    </form>
  </div>

  <!-- Scrollable comments list container -->
  <div class="comments-list-container">
    {#if loading && comments.length === 0}
      <div class="loading-state">
        <p>{$t('comments.loading')}</p>
      </div>
    {:else if comments.length === 0}
      <EmptyState title={$t('comments.noCommentsYet')} padding="1rem" />
    {:else}
      {#each comments as comment (comment.uuid)}
        <div class="comment-item" transition:slide={{duration: 300}}>
          <div class="comment-header">
            <div class="comment-user">
              {comment.user ? (comment.user.full_name || comment.user.email || `User ${comment.user.uuid}`) : $t('comments.anonymous')}
            </div>
            <div class="comment-timestamp">
              {#if comment.timestamp !== null}
                <button
                  type="button"
                  class="timestamp-link"
                  on:click={() => dispatch('seekTo', { time: comment.timestamp })}
                  on:keydown={(e) => e.key === 'Enter' && dispatch('seekTo', { time: comment.timestamp })}
                  aria-label={$t('comments.jumpToTimestamp', { time: formatTimestamp(comment.timestamp) })}
                  title={$t('comments.jumpToTime', { time: formatTimestamp(comment.timestamp) })}
                >
                  {formatTimestamp(comment.timestamp)}
                </button>
              {/if}
              <span>{new Date(comment.created_at).toLocaleDateString()}</span>
            </div>
          </div>

          {#if editingCommentId === comment.uuid}
            <form class="edit-comment-form" on:submit|preventDefault={() => editComment(comment.uuid, editingCommentText)}>
              <textarea
                bind:value={editingCommentText}
                rows="2"
              ></textarea>
              <div class="edit-buttons">
                <button
                  type="button"
                  class="cancel-button"
                  on:click={() => editingCommentId = null}
                  title={$t('comments.cancelEditHint')}
                >
                  {$t('comments.cancel')}
                </button>
                <button
                  type="submit"
                  class="save-button"
                  title={$t('comments.saveChangesHint')}
                >
                  {$t('comments.save')}
                </button>
              </div>
            </form>
          {:else}
            <div class="comment-body">
              <TruncatedText text={comment.text} maxLength={150} />
            </div>

            <!-- Only show edit/delete for comment author -->
            {#if isCommentAuthor(comment.user.uuid)}
              <div class="comment-actions">
                <button
                  type="button"
                  class="edit-button"
                  on:click={() => {
                    editingCommentId = comment.uuid;
                    editingCommentText = comment.text;
                  }}
                  title={$t('comments.editHint')}
                >
                  {$t('comments.edit')}
                </button>
                <button
                  type="button"
                  class="delete-button"
                  on:click={() => deleteComment(comment.uuid)}
                  title={$t('comments.deleteHint')}
                >
                  {$t('comments.delete')}
                </button>
              </div>
            {/if}
          {/if}
        </div>
      {/each}
    {/if}
  </div>
</div>

<!-- Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showConfirmModal}
  title={confirmModalTitle}
  message={confirmModalMessage}
  confirmText={$t('comments.delete')}
  cancelText={$t('comments.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={handleConfirmModalConfirm}
  on:cancel={handleConfirmModalCancel}
  on:close={handleConfirmModalCancel}
/>

<style>
  .comments-header-internal {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border-color);
    background: var(--bg-secondary, var(--surface-color));
    position: sticky;
    top: 0;
    z-index: 10;
  }

  .section-heading {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .comments-preview {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .tag-chip {
    background: var(--primary-light);
    color: var(--primary-color);
    padding: 3px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
    white-space: nowrap;
  }

  .no-comments {
    color: var(--text-secondary);
    font-size: 12px;
    font-style: italic;
  }

  /* Main wrapper for the entire comments component */
  .comments-wrapper {
    display: flex;
    flex-direction: column;
    gap: 0;
    max-height: 1200px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
  }

  /* Fixed comment form container at the top */
  .comment-form-container {
    position: sticky;
    top: 0;
    z-index: 5;
    padding: 1rem 0.5rem;
    border-bottom: 1px solid var(--border-color, rgba(120, 120, 120, 0.2));
  }

  /* Scrollable comments list container */
  .comment-form {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    width: 100%;
  }

  .comments-list-container {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem;
    max-height: 800px;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .comment-form {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    width: 100%;
    max-width: none;
  }

  textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 0.75rem;
    border: 1px solid var(--border-color, rgba(120, 120, 120, 0.2));
    border-radius: 8px;
    resize: vertical;
    min-height: 70px;
    font-family: inherit;
    font-size: 0.9rem;
    line-height: 1.5;
    background-color: var(--input-background, rgba(255, 255, 255, 0.05));
    color: var(--text-color);
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }

  textarea:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
  }



  .form-actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.75rem;
  }

  .timestamp-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    max-width: 100%;
    min-height: 2.5rem;
  }

  .timestamp-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    background-color: var(--button-secondary-bg, rgba(59, 130, 246, 0.1));
    border: 1px solid var(--button-secondary-border, rgba(59, 130, 246, 0.3));
    border-radius: 6px;
    color: var(--primary-color);
    padding: 0.5rem 0.75rem;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .timestamp-button:hover {
    background-color: var(--button-secondary-bg-hover, rgba(59, 130, 246, 0.2));
    border-color: var(--primary-color);
  }

  .button-icon {
    font-size: 1.1rem;
    display: flex;
    align-items: center;
  }

  .timestamp-link {
    background: none;
    border: 1px solid rgba(59, 130, 246, 0.2);
    padding: 0.2rem 0.4rem;
    border-radius: 4px;
    font-size: 0.85rem;
    color: var(--primary-color);
    cursor: pointer;
    transition: background-color 0.15s ease;
    font-weight: 500;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    white-space: nowrap;
  }

  .current-timestamp {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    background-color: var(--button-secondary-bg, rgba(59, 130, 246, 0.1));
    border: 1px solid var(--button-secondary-border, rgba(59, 130, 246, 0.3));
    border-radius: 6px;
    color: var(--primary-color);
    padding: 0.5rem 0.75rem;
    font-size: 0.85rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .timestamp-value {
    font-weight: 500;
    color: var(--primary-color);
  }

  .clear-button {
    background: none;
    border: none;
    color: var(--text-color-secondary);
    cursor: pointer;
    width: 1.5rem;
    height: 1.5rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    padding: 0;
    margin-left: 0.5rem;
    opacity: 0.7;
    transition: all 0.2s ease;
  }

  .clear-button:hover {
    color: var(--error-color);
    background-color: rgba(239, 68, 68, 0.1);
    opacity: 1;
    transform: scale(1.1);
  }

  button {
    padding: 0.35rem 0.75rem;
    border: none;
    background-color: #3b82f6;
    color: white;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 500;
    transition: background-color 0.2s;
    margin: 0;
    white-space: nowrap;
    font-size: 0.85rem;
  }

  button:hover {
    background-color: var(--primary-color-dark);
  }

  button:disabled {
    background-color: var(--disabled-color, #a0aec0);
    cursor: not-allowed;
  }

  .submit-button {
    background-color: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .submit-button:hover:not(:disabled) {
    background-color: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .submit-button:active:not(:disabled) {
    transform: scale(1);
  }

  .submit-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }


  .comment-item {
    padding: 0.75rem;
    border: 1px solid var(--border-color, rgba(120, 120, 120, 0.2));
    border-radius: 8px;
    background-color: var(--surface-color, rgba(255, 255, 255, 0.03));
    transition: border-color 0.2s ease;
    width: 100%;
    box-sizing: border-box;
  }

  .comment-item:hover {
    border-color: var(--border-color-hover, rgba(120, 120, 120, 0.3));
  }

  .comment-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
    font-size: 0.9rem;
  }

  .comment-timestamp {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--text-light);
    font-size: 0.8rem;
  }

  .timestamp-link {
    color: var(--primary-color);
    cursor: pointer;
  }

  .timestamp-link:hover {
    text-decoration: none;
    background-color: rgba(59, 130, 246, 0.1);
    color: var(--primary-color-dark);
    border-color: var(--primary-color);
  }

  .comment-body {
    font-size: 0.9rem;
    line-height: 1.5;
    margin-bottom: 0.5rem;
  }

  .comment-actions {
    display: flex;
    justify-content: flex-end;
  }

  .edit-comment-form {
    width: 100%;
    margin-top: 0.5rem;
  }

  .edit-comment-form textarea {
    width: 100%;
    margin-bottom: 0.5rem;
  }

  .edit-buttons {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }

  .save-button, .cancel-button {
    padding: 0.4rem 0.8rem;
    border-radius: 10px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    border: none;
    min-width: 60px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .save-button {
    background: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .save-button:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .save-button:active {
    transform: scale(1);
  }

  .cancel-button {
    background: #6b7280;
    color: white;
    border: 1px solid #6b7280;
    box-shadow: var(--card-shadow);
    transition: all 0.2s ease;
  }

  .cancel-button:hover {
    background: #4b5563;
    border: 1px solid #4b5563;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(75, 85, 99, 0.25);
  }

  .cancel-button:active {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(75, 85, 99, 0.2);
  }

  .edit-button {
    background: none;
    border: none;
    color: var(--primary-color);
    font-size: 0.8rem;
    cursor: pointer;
    padding: 0;
    margin-right: 0.5rem;
  }

  .delete-button {
    background: none;
    border: none;
    color: var(--error-color);
    font-size: 0.8rem;
    cursor: pointer;
    padding: 0;
  }

  .loading-state {
    padding: 1rem;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }
</style>
