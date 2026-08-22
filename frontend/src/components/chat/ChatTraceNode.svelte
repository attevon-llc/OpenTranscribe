<!--
  ChatTraceNode.svelte — one row of the query-execution trace (GH #514).

  ⚠️ The six outcomes must stay distinguishable with COLOUR REMOVED. Marker
  shape and the outcome word both carry the state, so the tree survives
  greyscale, a colourblind reader, and a screen reader equally. `empty` (a ring)
  versus `skipped` (a dash) is the pair the whole feature exists to separate —
  they differ by marker FAMILY, not by fill, because a fill difference is
  exactly what gets missed.

  Renders in EMISSION order, never canonical stage order: a stage that ran out
  of sequence should be visible, not tidied away.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import type { TraceNode } from '$lib/chat/traceTree';
  import { isPending } from '$lib/chat/traceTree';
  import {
    detailChips,
    formatMs,
    markerClass,
    outcomeLabelKey,
    reasonLabelKey,
    sourceLabelKey,
    stageLabelKey
  } from '$lib/chat/traceLabels';
  import ChatTraceTree from './ChatTraceTree.svelte';

  export let node: TraceNode;
  export let depth = 0;
  export let streaming = false;
  export let reducedMotion = false;
  export let visible: ReadonlySet<string> = new Set();

  $: pending = streaming && isPending(node);
  $: source = sourceLabelKey(node.detail);
  $: timing = formatMs(node.detail.ms);
  $: chips = detailChips(node);
  // The raw code is the fallback, so a `reason` the backend added after this
  // client shipped renders as itself rather than as a dotted key.
  $: reason = node.detail.reason
    ? $t(reasonLabelKey(node.detail.reason), { defaultValue: node.detail.reason })
    : null;
</script>

<li
  class="trace-node"
  class:trace-node--pending={pending}
  class:trace-node--static={reducedMotion}
  data-outcome={node.outcome}
  data-stage={node.stage}
  data-testid="trace-node"
>
  <div class="trace-row">
    <span class="trace-marker {markerClass(node.outcome)}" aria-hidden="true"></span>
    <span class="trace-label">{$t(stageLabelKey(node.stage))}</span>

    {#if source}
      <span class="trace-badge trace-badge--source">{$t(source)}</span>
    {/if}

    <span class="trace-badge trace-badge--outcome" data-testid="trace-outcome">
      {$t(outcomeLabelKey(node.outcome))}
    </span>

    {#each chips as chip (chip.key)}
      <span class="trace-chip">{$t(chip.key, chip.params)}</span>
    {/each}

    {#if reason}
      <span class="trace-reason">{reason}</span>
    {/if}

    {#if timing}
      <span class="trace-ms">{$t(timing.key, timing.params)}</span>
    {/if}
  </div>

  {#if node.children.length}
    <ChatTraceTree nodes={node.children} depth={depth + 1} {streaming} {reducedMotion} {visible} />
  {/if}
</li>

<style>
  .trace-node {
    position: relative;
    margin: 0;
    padding: 0.1rem 0;
    animation: trace-node-enter 200ms cubic-bezier(0.16, 1, 0.3, 1) both;
  }

  /* Reduced motion: the node's final state appears instantly, so the tree is
     literally readable as a static list rather than approximately so. */
  .trace-node--static {
    animation: none;
  }

  .trace-row {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    flex-wrap: wrap;
    font-size: 0.78rem;
    line-height: 1.6;
  }

  .trace-marker {
    flex: none;
    width: 9px;
    height: 9px;
    align-self: center;
  }

  /* Shape carries the outcome. Colour only reinforces it. */
  .trace-marker--dot {
    border-radius: 50%;
    background: var(--primary-color);
  }
  .trace-marker--ring {
    border-radius: 50%;
    border: 1.5px solid var(--text-secondary);
  }
  .trace-marker--dash {
    height: 2px;
    background: var(--text-secondary);
  }
  .trace-marker--square {
    border-radius: 2px;
    background: var(--primary-color);
  }
  .trace-marker--slash {
    border-radius: 50%;
    border: 1.5px solid var(--text-color);
    position: relative;
  }
  .trace-marker--slash::after {
    content: '';
    position: absolute;
    inset: -2px auto -2px 3px;
    width: 1.5px;
    background: var(--text-color);
    transform: rotate(45deg);
  }
  .trace-marker--cross {
    background: var(--error-color, #ef4444);
    clip-path: polygon(
      20% 0%,
      0% 20%,
      30% 50%,
      0% 80%,
      20% 100%,
      50% 70%,
      80% 100%,
      100% 80%,
      70% 50%,
      100% 20%,
      80% 0%,
      50% 30%
    );
  }

  .trace-label {
    color: var(--text-color);
    font-weight: 500;
  }

  /* A skipped step is de-emphasised but never hidden: "we never looked" is a
     finding, not an absence. */
  .trace-node[data-outcome='skipped'] > .trace-row {
    opacity: 0.55;
  }

  .trace-badge {
    padding: 0.02rem 0.3rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--surface-alt, var(--background-secondary));
    color: var(--text-secondary);
    font-size: 0.66rem;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .trace-node[data-outcome='failed'] .trace-badge--outcome {
    border-color: var(--error-color, #ef4444);
    color: var(--error-color, #ef4444);
  }

  .trace-chip,
  .trace-ms,
  .trace-reason {
    color: var(--text-secondary);
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
  }

  .trace-reason {
    font-style: italic;
  }

  .trace-ms {
    margin-left: auto;
    opacity: 0.75;
  }

  .trace-node--pending .trace-marker {
    animation: skeleton-pulse 1.4s ease-in-out infinite;
  }

  @media (prefers-reduced-motion: reduce) {
    .trace-node,
    .trace-node--pending .trace-marker {
      animation: none;
    }
  }
</style>
