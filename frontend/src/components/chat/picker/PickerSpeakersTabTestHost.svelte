<!--
  Test-only host — see ChatMessageMetaDisambiguateTestHost.svelte's docstring
  for why: Svelte 5 removed `component.$on(...)`, so a legacy
  `createEventDispatcher` event is only observable through an `on:event`
  listener wired at a consuming component's markup. FilePickerModal.svelte is
  that consumer in production; this host is that listener for
  PickerSpeakersTab.test.ts only.
-->
<script lang="ts">
  import PickerSpeakersTab from './PickerSpeakersTab.svelte';

  export let selected: string[] = [];
  export let onChange: (next: string[]) => void = () => {};
</script>

<PickerSpeakersTab {selected} on:change={(e) => onChange(e.detail)} />
