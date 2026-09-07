<!--
  ChatTracePanel.svelte — the collapsible query-execution trace (GH #514).

  ⚠️ `position: fixed`, NOT a third grid column. `.chat-page` is a two-column
  grid whose message column is independently centred at 52rem, so any grid track
  added here shrinks the answer — and "does not reflow the answer when it opens"
  is a requirement, not a preference. Escaping the grid box is the only
  mechanism that satisfies it, and it is what `NotificationsPanel` already does.

  Non-modal on purpose: this is an inspector meant to stay open while you read,
  like devtools, so there is no focus trap, no click-outside dismissal and no
  backdrop on desktop.

  ⚠️ Escape must stopPropagation. The page-level Escape handler CANCELS an
  in-flight generation, so without this, closing the panel would abort the answer.

  ⚠️ RTL: pinned to `right: 0` unconditionally rather than `inset-inline-end`. A
  right-hand inspector anchors to the physical screen edge in both directions —
  a deliberate deviation from the app's general RTL rule, recorded here so a
  future audit does not "fix" it into a regression. Text inside still follows dir.
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import type { TraceState } from '$lib/chat/traceTree';
  import { flattenTrace } from '$lib/chat/traceTree';
  import { RevealPacer } from '$lib/chat/revealPacer';
  import { escapeKey } from '$lib/actions/escapeKey';
  import ChatTraceTree from './ChatTraceTree.svelte';

  export let open = false;
  export let trace: TraceState | undefined = undefined;
  export let streaming = false;
  /** The turn this trace belongs to; a change resets the pacer. */
  export let turnId: string | null = null;
  /** True once the turn errored before any stage ran. */
  export let failedEarly = false;
  /** True when the turn deliberately searched no recordings. */
  export let contextOff = false;
  /**
   * Whether the conversation has any turn at all.
   *
   * Defaults to `true` so every existing call site keeps its current copy; the
   * page passes the real value. Without it, opening the panel on a brand-new
   * thread claims the trace "was not stored" for a question nobody asked.
   */
  export let hasTurn = true;

  const dispatch = createEventDispatcher<{ close: void }>();

  let reducedMotion = false;
  let pacer = new RevealPacer();
  let visible: ReadonlySet<string> = new Set();
  let ticker: ReturnType<typeof setInterval> | undefined;
  let lastTurnId: string | null = null;

  onMount(() => {
    // `prefers-reduced-motion` needs handling in JS as well as CSS: the global
    // rule in animations.css collapses CSS durations, but it cannot reach the
    // reveal pacer, which is plain JavaScript.
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const apply = (matches: boolean) => {
      reducedMotion = matches;
      pacer = new RevealPacer({ reducedMotion: matches });
      visible = new Set();
    };
    apply(query.matches);
    const onChange = (event: MediaQueryListEvent) => apply(event.matches);
    query.addEventListener('change', onChange);

    ticker = setInterval(() => {
      const released = pacer.tick();
      if (released.length) visible = new Set(pacer.visible);
    }, 25);

    return () => {
      query.removeEventListener('change', onChange);
      if (ticker) clearInterval(ticker);
    };
  });

  // A new turn starts a clean reveal; without this the previous turn's nodes
  // stay "already revealed" and the new tree appears fully-formed.
  $: if (turnId !== lastTurnId) {
    lastTurnId = turnId;
    pacer.reset();
    visible = new Set();
  }

  $: nodes = flattenTrace(trace);
  $: pacer.offer(nodes.map((node) => ({ key: node.key, parent: node.nodeId })));

  // The turn is over: release whatever is still queued rather than trailing it.
  $: if (!streaming && nodes.length) {
    const rest = pacer.finish();
    if (rest.length) visible = new Set(pacer.visible);
  }

  $: hasNodes = nodes.length > 0;
  // Always a string — it is only read in the `{:else}` branch, and returning
  // `null` there would only force a non-null assertion at the call site.
  //
  // The five states are deliberately distinct copy: "nothing has been asked
  // yet", "this turn failed before step 1", "context was off for this turn",
  // "waiting" and "traces are not stored" are five different facts, and
  // collapsing them would make a legitimately empty panel read as a bug.
  //
  // `noTurnYet` is checked FIRST because a thread with no turns satisfies none
  // of the others in any meaningful way — telling someone their trace "was not
  // stored" for a question they have not asked is simply wrong.
  $: emptyKey = !hasTurn
    ? 'chat.trace.empty.noTurnYet'
    : failedEarly
      ? 'chat.trace.empty.failedEarly'
      : contextOff
        ? 'chat.trace.empty.contextOff'
        : streaming
          ? 'chat.trace.empty.waiting'
          : 'chat.trace.empty.notStored';
</script>

{#if open}
  <!--
    `<aside>` already carries the `complementary` role implicitly, so spelling it
    out is redundant. `tabindex="-1"` makes the region programmatically
    focusable — which is what lets a container-scoped Escape handler be a real
    keyboard affordance rather than a listener stranded on inert markup.
  -->
  <aside
    class="trace-panel"
    tabindex="-1"
    aria-label={$t('chat.trace.title')}
    data-testid="chat-trace-panel"
    use:escapeKey={{ enabled: open, onEscape: () => dispatch('close') }}
  >
    <header class="trace-header">
      <h2 class="trace-title">{$t('chat.trace.title')}</h2>
      <!-- Sets expectations before a defect does. This panel reports on a
           retrieval pipeline that is itself under active development (#461), so
           a stage can legitimately read oddly while the thing it describes is
           being changed. The `title` carries the detail rather than spending
           header width on it. -->
      <span
        class="trace-beta"
        title={$t('chat.trace.betaTitle')}
        data-testid="chat-trace-beta"
      >
        {$t('chat.trace.beta')}
      </span>
      {#if streaming}
        <span class="trace-live" data-testid="chat-trace-live">{$t('chat.trace.live')}</span>
      {/if}
      <button
        type="button"
        class="trace-close"
        on:click={() => dispatch('close')}
        aria-label={$t('chat.trace.close')}
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </header>

    {#if trace?.truncated}
      <!-- Never dismissible: a shortened tree that does not say so is a trace
           that lies about what ran, which is the failure this panel exists for. -->
      <p class="trace-truncated" data-testid="chat-trace-truncated">
        {$t('chat.trace.truncated')}
      </p>
    {/if}

    <div class="trace-body">
      {#if hasNodes}
        <ChatTraceTree nodes={trace?.roots ?? []} {streaming} {reducedMotion} {visible} />
      {:else}
        <p class="trace-empty" data-testid="chat-trace-empty">{$t(emptyKey)}</p>
      {/if}
    </div>
  </aside>
{/if}

<style>
  .trace-panel {
    position: fixed;
    top: var(--navbar-height, 60px);
    right: 0;
    bottom: 0;
    z-index: 25;
    display: flex;
    flex-direction: column;
    width: min(24rem, 92vw);
    border-left: 1px solid var(--border-color);
    background-color: var(--surface-color);
    box-shadow: -8px 0 24px rgba(0, 0, 0, 0.12);
  }

  .trace-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.7rem 0.85rem;
    border-bottom: 1px solid var(--border-color);
  }

  .trace-title {
    margin: 0;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-color);
  }

  /* Deliberately quieter than `.trace-live`: an outlined neutral chip, not a
     coloured one. It is a standing caveat, and a permanent badge competing with
     the live indicator would train people to stop reading both. `help` cursor
     is what advertises that the title carries more. */
  .trace-beta {
    padding: 0.05rem 0.35rem;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    color: var(--text-secondary);
    font-size: 0.6rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    cursor: help;
    white-space: nowrap;
  }

  .trace-live {
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
    background-color: rgba(var(--primary-color-rgb), 0.14);
    color: var(--primary-on-surface);
    font-size: 0.64rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .trace-close {
    margin-left: auto;
    display: inline-flex;
    padding: 0.2rem;
    border: none;
    border-radius: 5px;
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .trace-close:hover {
    background-color: var(--surface-alt, var(--background-secondary));
    color: var(--text-color);
  }

  .trace-truncated {
    margin: 0;
    padding: 0.45rem 0.85rem;
    border-bottom: 1px solid var(--border-color);
    background-color: rgba(var(--primary-color-rgb), 0.06);
    color: var(--text-secondary);
    font-size: 0.72rem;
  }

  .trace-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 0.6rem 0.85rem 1.2rem;
  }

  .trace-empty {
    margin: 0;
    color: var(--text-secondary);
    font-size: 0.78rem;
    line-height: 1.5;
  }

  @media (max-width: 900px) {
    .trace-panel {
      width: 100vw;
    }
  }
</style>
