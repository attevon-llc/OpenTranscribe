<script>
  import { supportSessionGate } from '$stores/supportSession';
  import { onMount, createEventDispatcher } from 'svelte';
  // Use the shared axios instance so auth token is always sent
  import axiosInstance from '$lib/axios';
  import { authStore } from '$stores/auth';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import CommentComposer from '$components/comments/CommentComposer.svelte';
  import CommentItem from '$components/comments/CommentItem.svelte';
  import { sortComments } from '$components/comments/commentOrder';
  import {
    CommentEndpointsExhausted,
    fetchFileComments,
    postFileComment,
    withUser
  } from '$components/comments/commentRequests';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import EmptyState from './ui/EmptyState.svelte';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { fileFrozen } from '$lib/legalHold/fileHoldContext';
  import { reportHoldRefusal } from '$lib/legalHold/refusal';
  import { t } from '$stores/locale';

  // The Svelte component is exported by default automatically

  /** @type {string} */
  export let fileId = "";
  /** @type {number} */
  export let currentTime = 0;
  /** @type {number | null} Media length in seconds, when known; bounds a typed comment time. */
  export let duration = null;
  $: targetId = fileId;
  $: commentsEndpoint = `/comments/files/${targetId}/comments`;

  /** @type {Array<{uuid: string, text: string, timestamp: number | null, user: {uuid: string, email?: string, full_name?: string, username?: string}, created_at: string}>} */
  let comments = [];
  /** @type {boolean} */
  let loading = true;

  // Confirmation modal state
  // A held file keeps taking NEW comments (recorded as hold annotations); existing ones are
  // frozen (legal-hold plan, Lane D).
  const frozen = fileFrozen();

  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
  } = confirmDialog;

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

  /** @param {any} error */
  function logFetchError(error) {
    console.error('Error fetching comments:', error?.message, error?.response?.status, error?.response?.data);
  }

  /** @param {any} error */
  function reportFetchFailure(error) {
    if (error instanceof CommentEndpointsExhausted) {
      console.error('[CommentSection] All endpoints failed');
      toastStore.error($t('comments.loadFailed', { error: getErrorMessage(error.cause, /** @type {any} */ (error.cause)?.message) }));
    } else if (error.response?.status === 401) {
      // An authentication issue
      toastStore.error($t('comments.loginRequired'));
    } else {
      toastStore.error($t('comments.loadFailed', { error: getErrorMessage(error, error?.message) }));
    }
  }

  async function fetchComments() {
    loading = true;
    try {
      // Validate the target id
      if (!targetId) {
        console.error('Invalid target ID for comments:', targetId);
        toastStore.error($t('comments.invalidFileId'));
        loading = false;
        return;
      }

      let response;
      try {
        response = await fetchFileComments(axiosInstance, fileId, logFetchError);
      } catch (/** @type {any} */ error) {
        reportFetchFailure(error);
        return;
      }

      // A comment with no user info gets one from the auth store
      comments = sortComments(
        response.data.map(/** @param {any} comment */ (comment) =>
          withUser(comment, $authStore.user, comment.user_id)
        )
      );
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
        toastStore.error($t('comments.loadFailed', { error: getErrorMessage(err, err?.message) }));
      }
    } finally {
      loading = false;
    }
  }

  /** @param {any} error */
  function logAddError(error) {
    // Log detailed error information for debugging
    console.error('[CommentSection] Error adding comment:', error);
    console.error('[CommentSection] Error response:', error.response?.status, error.response?.data);
  }

  /** @param {any} error */
  function reportAddFailure(error) {
    if (error instanceof CommentEndpointsExhausted) {
      console.error('[CommentSection] All endpoints failed for adding comment');
      toastStore.error($t('comments.addFailed', { error: getErrorMessage(error.cause, /** @type {any} */ (error.cause)?.message) }));
    } else if (error.response?.status === 401) {
      // An authentication issue
      toastStore.error($t('comments.loginRequiredToAdd'));
    } else {
      toastStore.error($t('comments.addFailed', { error: getErrorMessage(error, error?.message) }));
    }
  }

  /**
   * Post a comment from the composer. `timestamp` is null for a general comment.
   * @param {{text: string, timestamp: number | null}} payload
   * @returns {Promise<boolean>} whether the comment was saved
   */
  async function addComment({ text: newComment, timestamp }) {
    try {
      // Get user data from auth store
      const currentUser = $authStore.user;

      let response;
      try {
        // Cookies handle authentication automatically
        response = await postFileComment(axiosInstance, fileId, newComment, timestamp, logAddError);
      } catch (/** @type {any} */ error) {
        reportAddFailure(error);
        return false;
      }

      // Ensure the comment has a user object, from the auth store if the response had none
      const commentWithUser = withUser(
        response.data,
        currentUser,
        response.data.user_id || currentUser?.uuid || ''
      );

      comments = sortComments([...comments, commentWithUser]);
      dispatch('commentAdded', commentWithUser);
      return true;
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
        toastStore.error($t('comments.addFailed', { error: getErrorMessage(err, err?.message) }));
      }
      return false;
    }
  }

  /**
   * Save an edit. The kind travels explicitly: `timestamp: null` makes it a general comment.
   * @param {string} commentId - The UUID of the comment to edit
   * @param {{text: string, timestamp: number | null}} payload
   * @returns {Promise<boolean>} whether the edit was saved
   */
  async function editComment(commentId, { text: newText, timestamp }) {
    if (!newText || !newText.trim()) {
      toastStore.error($t('comments.textCannotBeEmpty'));
      return false;
    }

    try {
      await axiosInstance.put(`/comments/${commentId}`, { text: newText, timestamp });
      comments = sortComments(
        comments.map(c => c.uuid === commentId ? { ...c, text: newText, timestamp } : c)
      );
      return true;
    } catch (/** @type {any} */ err) {
      console.error('[CommentSection] Error editing comment:', err);
      console.error('[CommentSection] Error details:', {
        message: err.message,
        response: err.response,
        status: err.response?.status,
        data: err.response?.data
      });

      // Provide a more user-friendly error message
      if (reportHoldRefusal(err)) return false;
      if (err.response && err.response.status === 401) {
        toastStore.error($t('comments.loginRequiredToEdit'));
      } else {
        toastStore.error($t('comments.editFailed', { error: getErrorMessage(err, err?.message) }));
      }
      return false;
    }
  }

  /**
   * Delete a comment
   * @param {string} commentId - The UUID of the comment to delete
   */
  async function deleteComment(commentId) {
    confirmDialog.request(
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
      if (reportHoldRefusal(err)) return;
      if (err.response && err.response.status === 401) {
        toastStore.error($t('comments.loginRequiredToDelete'));
      } else {
        toastStore.error($t('comments.deleteFailed', { error: getErrorMessage(err, err?.message) }));
      }
    }
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
  {#if !$supportSessionGate.readOnly}
  <div class="comment-form-container">
    <CommentComposer {currentTime} {duration} submit={addComment} />
    {#if $frozen}
      <p class="hold-comment-hint" data-testid="hold-comment-hint">{$t('holds.commentHint')}</p>
    {/if}
  </div>
  {/if}

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
        <CommentItem
          {comment}
          {currentTime}
          {duration}
          isAuthor={isCommentAuthor(comment.user?.uuid)}
          frozen={$frozen}
          save={(payload) => editComment(comment.uuid, payload)}
          on:seekTo={(e) => dispatch('seekTo', e.detail)}
          on:delete={() => deleteComment(comment.uuid)}
        />
      {/each}
    {/if}
  </div>
</div>

<!-- Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$t('comments.delete')}
  cancelText={$t('comments.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .hold-comment-hint {
    margin: var(--space-2) 0 0;
    font-size: var(--font-size-xs);
    color: var(--color-warning-text);
  }

  .comments-header-internal {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--border-color);
    background: var(--bg-secondary);
    position: sticky;
    top: 0;
    z-index: var(--z-sticky);
  }

  .section-heading {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .comments-preview {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }

  .tag-chip {
    background: var(--primary-light);
    color: var(--primary-on-surface);
    padding: 3px var(--space-2);
    border-radius: var(--radius-2xl);
    font-size: 11px;
    font-weight: 500;
    white-space: nowrap;
  }

  .no-comments {
    /* Sits on the slate-100 secondary surface, where --text-secondary is 4.34:1 —
     * under WCAG AA, and italic 12px is the least legible text on the page. */
    color: var(--text-on-tint);
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
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  /* Fixed comment form container at the top */
  .comment-form-container {
    position: sticky;
    top: 0;
    z-index: 5;
    padding: var(--space-4) var(--space-2);
    border-bottom: 1px solid var(--border-color);
  }

  .comments-list-container {
    flex: 1;
    overflow-y: auto;
    padding: var(--space-2);
    max-height: 800px;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .loading-state {
    padding: var(--space-4);
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }
</style>
