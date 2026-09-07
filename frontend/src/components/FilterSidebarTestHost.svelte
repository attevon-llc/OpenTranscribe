<!--
  Test-only host — Svelte 5 removed `component.$on(...)`, so a legacy
  `createEventDispatcher` event is only observable through an `on:event`
  listener wired in a consuming component's markup. The gallery route is that
  consumer in production; this host is that listener for FilterSidebar.test.ts.
-->
<script lang="ts">
  import FilterSidebar from './FilterSidebar.svelte';
  export let onFilter: (detail: unknown) => void = () => {};
  export let onReset: () => void = () => {};
</script>

<FilterSidebar on:filter={(e) => onFilter(e.detail)} on:reset={onReset} />
