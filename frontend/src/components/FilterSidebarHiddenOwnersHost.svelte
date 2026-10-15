<!--
  Test-only host: the search page renders FilterSidebar with the owner filter
  hidden (`GET /search` has no owner predicate). A dedicated host pins that one
  configuration without widening FilterSidebarTestHost.
-->
<script lang="ts">
  import FilterSidebar from './FilterSidebar.svelte';
</script>

<FilterSidebar showOwnerFilter={false} />
