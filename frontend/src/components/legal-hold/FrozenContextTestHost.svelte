<script lang="ts">
  // Test double, not a product component: renders `component` under the file page's legal-hold
  // context (`provideFileFrozen`), so a child's frozen state can be driven without the page.
  import type { ComponentType, SvelteComponent } from 'svelte';
  import { provideFileFrozen } from '$lib/legalHold/fileHoldContext';

  export let frozen = false;
  export let component: ComponentType<SvelteComponent>;
  export let props: Record<string, unknown> = {};

  const store = provideFileFrozen();
  $: store.set(frozen);
</script>

<svelte:component this={component} {...props} />
