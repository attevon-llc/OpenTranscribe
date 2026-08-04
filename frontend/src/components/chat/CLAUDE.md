# Chat UI (issue #52)

The `/chat` route (`routes/chat/[[conversationId]]/`) is a coordinator only —
layout, guards, and route↔store sync. Behaviour lives in these components and in
`$stores/chat`.

`[[conversationId]]` is optional so `/chat` (fresh, no server row yet) and
`/chat/{uuid}` are the same page. **The conversation row is created lazily on
first send**, which keeps "opened chat, changed my mind" visits out of the
history sidebar.

## ⚠️ Two rendering paths, deliberately different

- **Assistant** messages → `ChatMarkdown` → `renderChatMarkdown()`.
- **User** messages → plain text with `white-space: pre-wrap`, **never** markdown.

There is no reason to interpret markup someone typed at themselves, and not
doing so removes a class of self-XSS.

`renderChatMarkdown` (`$lib/utils/chatMarkdown.ts`) is the XSS boundary and has
its **own** DOMPurify profile. Do not widen `sanitizeHighlightHtml` to fit chat —
that would loosen sanitization everywhere else.

Its non-obvious rule: `ALLOWED_URI_REGEXP` allows only `http(s):` and `mailto:`,
which **blocks relative URLs**. That is intentional — model text must never be
able to mint an app-internal link (`/settings`, `/files/…`) that looks like it
came from OpenTranscribe. Citations are rendered separately, from the structured
`sources` frame, via `citationHref()`.

## Streaming

`$lib/api/chatStream.ts` uses **fetch + ReadableStream, not EventSource**:

- the message body is POSTed, so prompts never reach URLs or access logs;
- `AbortController` gives a real stop control;
- EventSource auto-reconnect would silently re-trigger a whole billed generation.

`createSseParser()` is exported as a pure function precisely so it can be tested
against what a real network does — frames split mid-line, CRLF, multi-line
`data:`, keepalive comments, and unknown events from a newer backend (ignored,
not fatal).

Raw fetch bypasses the axios interceptors, so CSRF (`getCsrfToken()`) and the
one-shot 401 refresh are handled explicitly in that module.

## State machine

`$lib/utils/chatStateMachine.ts` is a pure module, separate from the store,
because the store imports SvelteKit navigation (unresolvable in vitest) and these
rules are the part most worth testing. The invariant: a late or duplicated frame
must never reopen a finished stream and strand the composer showing "Stop".

Exactly ONE stream is in flight at a time.

## Component map

| Component             | Note                                                                                                                      |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `ChatThread`          | Auto-scroll follows the stream **only while already at the bottom**; scrolling up suppresses follow and shows a jump pill |
| `ChatMessage`         | Hover-revealed actions; permanently visible on touch (`@media (hover: none)`)                                             |
| `ChatMarkdown`        | Re-parses the full buffer per throttled tick (rAF + 100ms floor), one final unthrottled render on completion              |
| `ChatSources`         | Citation cards; hrefs from structured data only                                                                           |
| `ChatComposer`        | Enter sends, Shift+Enter newline; send button **morphs** to Stop rather than disabling                                    |
| `ChatContextBar`      | Empty scope shows "All transcripts" explicitly; context-off gets its own chip                                             |
| `FilePickerModal`     | Edits a **draft**, commits on Confirm — scope changes rewrite what every later answer is based on                         |
| `ChatStatusIndicator` | The ONLY `aria-live` region; announcing the token stream would read the answer character by character                     |

## Conventions

- Svelte **4 idiom** (`export let`, `$:`, `createEventDispatcher`, `on:click`) —
  Svelte 5 is installed but the codebase has not migrated.
- Global button classes only (`.btn`, `.modal-button` + `.modal-primary-button` /
  `.modal-cancel-button`). No local button CSS.
- All colors via CSS vars; light/dark parity is mandatory.
- Stable `data-testid` hooks are consumed by `backend/tests/e2e/test_chat.py` —
  renaming one breaks E2E.
