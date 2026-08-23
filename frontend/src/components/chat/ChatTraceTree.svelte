<!--
  ChatTraceTree.svelte — the recursive list layer of the query trace (GH #514).

  A plain nested <ul>, deliberately NOT `role="tree"`. The ARIA tree pattern
  requires a roving tabstop and arrow-key navigation; this panel is read-only by
  design, so claiming `tree` would announce "tree, N items" to a screen-reader
  user who then finds arrow keys do nothing. A nested list is both semantically
  correct — it IS a hierarchical list — and interaction-free by default.

  `visible` is the reveal pacer's decision. A node not yet released is simply not
  rendered, which is what turns a burst of sixteen simultaneous frames into a
  cascade instead of a flicker.
-->
<script lang="ts">
  import type { TraceNode } from '$lib/chat/traceTree';
  import ChatTraceNode from './ChatTraceNode.svelte';

  export let nodes: TraceNode[] = [];
  export let depth = 0;
  export let streaming = false;
  export let reducedMotion = false;
  export let visible: ReadonlySet<string> = new Set();

  $: shown = nodes.filter((node) => visible.has(node.key));
</script>

<ul class="trace-list" data-depth={depth}>
  {#each shown as node (node.key)}
    <ChatTraceNode {node} {depth} {streaming} {reducedMotion} {visible} />
  {/each}
</ul>

<style>
  .trace-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  /* Nested levels get a rail, which is what makes a fan-out read as a branch
     rather than as a flat run of siblings. */
  .trace-list:not([data-depth='0']) {
    margin-left: 0.32rem;
    padding-left: 0.6rem;
    border-left: 1px solid var(--border-color);
  }
</style>
