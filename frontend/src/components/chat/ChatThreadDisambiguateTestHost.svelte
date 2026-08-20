<!--
  Test-only host: Svelte 5 removed the imperative `component.$on(...)` API, so a
  legacy `createEventDispatcher` event can only be observed through an `on:event`
  listener wired at the CONSUMING component's markup — exactly how the real app
  observes it (`routes/chat/[[conversationId]]/+page.svelte` is that consumer in
  production). This host is that listener, for
  ChatThread.disambiguate.test.ts's forwarding-chain test only: it proves the
  event survives BOTH hops (ChatMessage -> ChatThread), not just the first one
  ChatMessageMetaDisambiguateTestHost already covers.
-->
<script lang="ts">
  import ChatThread from './ChatThread.svelte';
  import type { ChatMessage as ChatMessageType, StreamStatus } from '$lib/types/chat';

  export let messages: ChatMessageType[] = [];
  export let status: StreamStatus = 'idle';
  export let onDisambiguate: (name: string) => void = () => {};
</script>

<ChatThread {messages} {status} on:disambiguate={(e) => onDisambiguate(e.detail)} />
