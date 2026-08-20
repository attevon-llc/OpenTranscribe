<!--
  Test-only host: Svelte 5 removed the imperative `component.$on(...)` API
  (`component_api_changed`), so a legacy `createEventDispatcher` event can
  only be observed through an `on:event` listener wired at the CONSUMING
  component's markup, exactly as it works in the real app (ChatMessage.svelte
  is that consumer in production). This host is that listener, for
  ChatMessageMeta.test.ts's disambiguation-chip test only.
-->
<script lang="ts">
  import ChatMessageMeta from './ChatMessageMeta.svelte';
  import type { ChatMessage } from '$lib/types/chat';

  export let message: ChatMessage;
  export let onDisambiguate: (name: string) => void = () => {};
</script>

<ChatMessageMeta {message} on:disambiguate={(e) => onDisambiguate(e.detail)} />
