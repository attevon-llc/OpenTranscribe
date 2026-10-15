<script lang="ts">
  import LazySection from '$components/settings/LazySection.svelte';
  import type { ComponentLoader } from '$lib/settings/sectionLoaders';

  export let load: ComponentLoader;
  export let label = 'host';
</script>

<LazySection {load} let:Component>
  <Component {label} />
</LazySection>
