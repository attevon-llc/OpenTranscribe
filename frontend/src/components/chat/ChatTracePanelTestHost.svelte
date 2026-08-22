<!--
  Test-only host — see ChatContextBarTestHost.svelte for the same reasoning:
  Svelte 5 removed `component.$on(...)`, so a legacy `createEventDispatcher`
  event is only observable through an `on:event` listener wired in a consuming
  component's markup. The /chat page is that consumer in production; this host
  is that listener for ChatTracePanel.test.ts's `close` cases.
-->
<script lang="ts">
  import ChatTracePanel from './ChatTracePanel.svelte';
  import type { TraceState } from '$lib/chat/traceTree';

  export let open = true;
  export let trace: TraceState | undefined = undefined;
  export let streaming = false;
  export let onClose: () => void = () => {};
</script>

<ChatTracePanel {open} {trace} {streaming} on:close={onClose} />
