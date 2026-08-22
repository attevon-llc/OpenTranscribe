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

**A new server frame is invisible until you add it to the parser's `known`
list.** That forward-compatibility rule silently drops anything unrecognised, so
a backend-only change ships a frame nobody ever sees. Frames today: `start`,
`status`, `sources`, `warning`, `trace`, `delta`, `reasoning`, `usage`, `done`,
`error`. `backend/tests/unit/test_chat_sse_contract.py` asserts the backend's
emitted set is a SUBSET of that array, so the two sides must land together.

`warning` (`{code, retrieved}`) reports that the answer is NOT grounded in the
user's recordings, and carries one of **three** codes:

- `context_dropped` (#384) — retrieval found excerpts and none fit the model's
  context window.
- `no_context` (#438) — nothing reached the prompt at all, and the search itself
  ran and genuinely found nothing (or masking dropped every chunk it found);
  the `retrieved` count separates the two.
- `retrieval_failed` (#438's open half, closed) — nothing reached the prompt
  because the chunk-plane search itself raised or had no OpenSearch client,
  distinct from `no_context` where the search ran. `services/search/
chunk_retrieval.retrieve_chunks` sets this via a `diagnostics` out-param on
  the no-client and exception branches only, never on a legitimately empty
  result — its absence is the "ordinary empty search" signal.

The store folds all three into `msg_metadata` (`context_dropped` /
`no_context` / `retrieval_failed`) rather than holding separate stream state,
so `ChatMessage` has **one** render path and the notice survives a reload; the
server persists the same flags on the message row. The three notices are an
`{#if}`/`{:else if}`/`{:else if}`, never stacked — they name different defects
and only one can be true. `sources` carries only the excerpts that actually
reached the prompt.

**A new warning `code` is as invisible as a new frame** if the client does not
know it: the store's fold checks the code explicitly, so an unhandled one is
parsed, ignored, and never rendered.

Raw fetch bypasses the axios interceptors, so CSRF (`getCsrfToken()`) and the
one-shot 401 refresh are handled explicitly in that module.

### `reasoning` frame (collapsible reasoning display)

Shaped identically to `delta` (`{"text": ...}`) but accumulated onto
`message.reasoning_content` — a field kept **strictly separate** from `content`
so reasoning text can never get mixed into the rendered answer. `chat.ts`'s
`applyEvent` also tracks three client-only fields for the live UI —
`reasoningStreaming`, `reasoningStartedAt`, `reasoningDurationMs` — deliberately
none of them named anything with "thinking" in it: `StreamStatus` already has a
value literally called `'thinking'` (see below) that means something unrelated,
and reusing the word here would be read as the same concept. The reasoning phase
ends (freezing `reasoningDurationMs`) on the first `delta` frame, or on
`done`/`error`/an aborted stream if no answer content ever arrived.

### ⚠️ The reasoning TOGGLE renders only where the off-switch was MEASURED (#64)

`ChatControlsPanel` offers a "Model reasoning" checkbox (`chat-reasoning-toggle`, in the
`advanced` block) **only** when the server reports `reasoning_off_switch === 'works'` for the
configuration in play — the conversation's pinned one, else the account default, read from
`LLMSettingsApi.getUserConfigurations()`. Every other verdict, a uuid missing from the map, and
a failed lookup all render **nothing**.

Do not relax that to "the provider accepts the parameter". Measured against a real vLLM serving
gemma-4-e4b, `enable_thinking: false` returned HTTP 200 and produced 931 characters of reasoning
— byte-identical to not sending the parameter at all. A toggle there tells the user reasoning is
off while the model reasons anyway, which is worse than no toggle: it is a false claim rather
than a missing feature. The verdict comes from a probe, per model; the whole design and the
numbers are in `backend/app/services/CLAUDE.md`.

Unchecking it writes `ConversationSettings.reasoning = false`; re-checking writes `null`
("inherit"). The server re-checks the capability at send time, so a preference stored against a
model that could honour it is silently not applied after a model switch.

Note this is a **different thing** from `ChatReasoning`'s local `expanded`, which only collapses
reasoning text that has already arrived.

## State machine

`$lib/utils/chatStateMachine.ts` is a pure module, separate from the store,
because the store imports SvelteKit navigation (unresolvable in vitest) and these
rules are the part most worth testing. The invariant: a late or duplicated frame
must never reopen a finished stream and strand the composer showing "Stop".

Exactly ONE stream is in flight at a time.

## Component map

| Component                         | Note                                                                                                                                                                                                                                                                                                 |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ChatThread`                      | Auto-scroll follows the stream **only while already at the bottom**; scrolling up suppresses follow and shows a jump pill                                                                                                                                                                            |
| `ChatMessage`                     | Hover-revealed actions; permanently visible on touch (`@media (hover: none)`)                                                                                                                                                                                                                        |
| `ChatMarkdown`                    | Re-parses the full buffer per throttled tick (rAF + 100ms floor), one final unthrottled render on completion                                                                                                                                                                                         |
| `ChatReasoning`                   | Collapsed-by-default reasoning/"thinking" block above `ChatMarkdown`; wraps `ui/ExpandableSection` + reuses `ChatMarkdown` for its body — never a second markdown pipeline. Rendered only when `message.reasoning_content` is non-empty                                                              |
| `ChatSources`                     | Citation cards; hrefs from structured data only                                                                                                                                                                                                                                                      |
| `ChatComposer`                    | Enter sends, Shift+Enter newline; send button **morphs** to Stop rather than disabling                                                                                                                                                                                                               |
| `ChatContextBar`                  | Empty scope shows "All transcripts" explicitly; context-off gets its own chip                                                                                                                                                                                                                        |
| `FilePickerModal`                 | Edits a **draft**, commits on Confirm — scope changes rewrite what every later answer is based on                                                                                                                                                                                                    |
| `ChatStatusIndicator`             | The ONLY `aria-live` region; announcing the token stream would read the answer character by character                                                                                                                                                                                                |
| `ChatTracePanel`                  | GH #514's query trace. Fixed overlay, NOT a grid column — `.chat-page`'s message column is centred at 52rem, so a third track would reflow the answer. Non-modal (no focus trap, no click-outside): an inspector you keep open, like devtools. Owns the reveal pacer and the reduced-motion listener |
| `ChatTraceTree` / `ChatTraceNode` | A plain nested `<ul>`, deliberately not `role="tree"` — that pattern promises arrow-key navigation this read-only panel does not have. Adds no second `aria-live` region                                                                                                                             |
| `ChatEmptyState`                  | Hosts `$components/RetrievalQualityNotice` (#461) — the ONE place in chat where that notice renders exactly once. `ChatSources` was the other candidate and was rejected: it renders per assistant message, so three expanded source lists would stack three copies of the same sentence             |

### The `trace` frame and its panel (GH #514)

One frame per stage: `{seq, stage, outcome, parent, node_id, detail}`. `detail`
stays a NESTED object rather than spread flat, so a future allowlist key can
never collide with a frame-level one.

Four rules live in `$lib/chat/traceTree.ts`, each because the naive version is
wrong:

- **A leg reports itself TWICE under one `node_id`** — `legs.py` emits a
  `fanned_*` when it starts and a `found` when it finishes. Those collapse into
  ONE node that advances, never two rows; that is also what gives every node a
  pending → resolved transition to animate.
- **Never regress a node's stage** on a late frame, but always merge `detail`,
  so the `plane` from the start frame survives the finish frame's counts.
- **Orphans are parked and re-parented**, never dropped — a fan-out completes
  out of order by design.
- **Never mutate a node in place.** Svelte reactivity keys off object identity,
  and the fold's own tests would pass while the DOM never repainted.

⚠️ **`empty` and `skipped` must never render alike.** They differ by marker
FAMILY (a hollow ring versus a dash), not by fill — a fill difference is exactly
what low-vision and colourblind readers miss, and that pair is the whole reason
the panel exists.

⚠️ **The reveal pacer is what stops a flicker.** On a cached turn all ~16 frames
land inside ~5ms (measured), so revealing each on arrival is a flash, not a
sequence. `$lib/chat/revealPacer.ts` decouples arrival from reveal. Reduced
motion needs THREE fixes: `animations.css` collapses CSS durations, but it
reaches neither a Svelte transition (Web Animations API) nor the pacer (plain
JS).

Escape uses `$lib/actions/escapeKey`, whose `stopPropagation` is load-bearing —
the page listens on `svelte:window` and uses Escape to CANCEL generation, so
without it closing the panel would abort the answer.

Traces are **live only**, so a reloaded conversation legitimately has none. The
panel says "not stored" rather than rendering blank, because a blank panel gets
reported as a bug.

## ⚠️ Events must be forwarded at EVERY hop

`ChatThread` sits between `ChatMessage` and the page. A `dispatch(...)` in
`ChatMessage` reaches `chatStore` only if `ChatThread` re-emits it — Svelte does
not bubble component events.

This bit us: `ChatThread` forwarded `regenerate` and `retry` but not `edit`, so
**edit-and-resend did nothing at all** — the editor closed, the question stayed
as it was, and no request was ever sent. It went unnoticed because the E2E test
covering it had never run (it self-skipped without an LLM). Use bare `on:edit`
forwarding rather than a wrapping handler unless you need to transform.

When adding an event to `ChatMessage`, check all three: dispatch in the child,
forward in `ChatThread`, handle on the page.

## Projects in the sidebar (issue #360)

`ChatSidebar` renders project groups above the date-grouped list. Conversations
with a `project_uuid` appear ONLY under their project, so nothing is listed
twice. Project sections are collapsed by default.

"New chat in this project" does NOT create a row: the conversation is still
created lazily on first send, with `chatStore.setPendingProject(uuid)` holding
the target so an abandoned visit leaves nothing behind — same reasoning as
`newConversation()`.

## Accessibility invariants

These are easy to regress and hard to notice without a keyboard:

- **`ChatControlsPanel`** uses `focusTrap` + `clickOutside` (`$lib/actions/`) and stops
  its own Escape from propagating — otherwise closing the panel would also cancel an
  in-flight generation via the page-level Escape handler.
- **The mobile drawer is `inert` when closed.** It is hidden with `transform` only, so
  without `inert` a keyboard or screen-reader user tabs straight into an invisible
  sidebar. The `inert` flag is driven by a `matchMedia` listener mirroring the 900px
  CSS breakpoint — above it the sidebar is a normal visible column and must stay
  reachable.
- **Inline editors return focus** to the button that opened them (`ChatMessage` edit,
  `ConversationListItem` rename). Dropping focus to `<body>` loses the user's place.
- **Code-block copy is a real `<button>`**, injected after each render. It was once a
  CSS `::before` activated by click coordinates — invisible to screen readers and
  unreachable by keyboard. It stays visible while focused, not just on hover.
- Never `outline: none` without a `:focus-visible` replacement.

## Conventions

- Svelte **4 idiom** (`export let`, `$:`, `createEventDispatcher`, `on:click`) —
  Svelte 5 is installed but the codebase has not migrated.
- Global button classes only (`.btn`, `.modal-button` + `.modal-primary-button` /
  `.modal-cancel-button`). No local button CSS.
- All colors via CSS vars; light/dark parity is mandatory.
- Stable `data-testid` hooks are consumed by `backend/tests/e2e/test_chat.py` —
  renaming one breaks E2E.
