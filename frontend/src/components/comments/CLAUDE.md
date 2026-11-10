# frontend/src/components/comments

Presentational children of `CommentSection.svelte` (the coordinator: fetch, post, edit, delete,
toasts). Children receive props and report up; they never call the API.

- `CommentComposer.svelte` — the new-comment form. **Timed by default**: the **Timestamp** box
  is ticked and the time follows the player until the first keystroke, then is pinned (it must
  not drift while the user types; video keeps playing). Unticked = a **general** comment
  (`timestamp: null`). The line under the box always names the kind ("Comment at 1:23" /
  "General comment on this file"). Add is never disabled for missing input: empty text shows an
  inline `role="alert"` message. Enter sends, Shift+Enter is a newline, IME Enter never sends
  (same as `chat/ChatComposer.svelte`). `submit` resolves true on success; only then it clears.
- `CommentTimeField.svelte` — the editable time (`m:ss` / `h:mm:ss` via `parseClock` in
  `$lib/utils/formatting`), −1 s / +1 s nudges, "Use current time". Validated against the media
  `duration`; the parent calls `validate()` before saving. An untouched box keeps the exact
  sub-second time (re-parsing its rounded label would move the comment).
- `CommentItem.svelte` — one listed comment: a time chip that seeks (`seekTo`) or a **General**
  label; the editor reuses the Timestamp box and `CommentTimeField`, so editing keeps the kind
  unless the user changes it, and always sends `timestamp` explicitly (`null` = general).
- On a file under a legal hold (`fileFrozen()` context) the composer stays enabled with
  `holds.commentHint` (a new comment is recorded as `content.hold.annotation`); `CommentItem`
  takes `frozen` and shows Edit/Delete disabled with a `FrozenNote`.
- `CommentKindHelp.svelte` — the info button + panel explaining the two kinds.
- `commentOrder.ts` — the one ordering rule: general first (oldest first), then timed in time
  order. Do not sort with `a.timestamp - b.timestamp`: `null` reads as 0 and mixes the kinds.

E2E selectors (`backend/tests/e2e/test_file_comments.py`): `.comment-form`, `button.submit-button`,
`.form-error`, `.timestamp-toggle input`, `.kind-summary.general`, `.comment-item`,
`button.timestamp-link`, `.general-label`, `button.btn-danger-text`.
