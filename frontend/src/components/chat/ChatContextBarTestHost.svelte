<!--
  Test-only host — see ChatMessageMetaDisambiguateTestHost.svelte's docstring
  for why: Svelte 5 removed `component.$on(...)`, so a legacy
  `createEventDispatcher` event is only observable through an `on:event`
  listener wired at a consuming component's markup. The /chat page is that
  consumer in production; this host is that listener for
  ChatContextBar.test.ts's `removeSpeaker` case only.
-->
<script lang="ts">
  import ChatContextBar from './ChatContextBar.svelte';
  import type { ChatScope } from '$lib/types/chat';

  export let scope: ChatScope;
  export let onRemoveSpeaker: (name: string) => void = () => {};
</script>

<ChatContextBar {scope} on:removeSpeaker={(e) => onRemoveSpeaker(e.detail)} />
