<!--
  LazySection.svelte — loads one settings section's component on demand.

  The modal renders each section's heading immediately and hands the heavy panel to this
  wrapper, which fetches the component's chunk, shows a skeleton while it is in flight and an
  error with a retry if it fails. The host supplies the loaded component through the default
  slot (`let:Component`), so it keeps its own props and event handlers:

    <LazySection load={sectionLoaders.download} let:Component>
      <Component on:navigate={...} />
    </LazySection>
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import type { ComponentLoader } from '$lib/settings/sectionLoaders';

  export let load: ComponentLoader;
  /** Skeleton rows while the chunk is in flight. */
  export let lines = 3;
  /** Shape of the skeleton; match what the panel will look like. */
  export let variant: 'form' | 'rows' | 'chips' | 'dashboard' = 'form';

  let loaded: { loader: ComponentLoader; component: any } | null = null;
  let failedLoader: ComponentLoader | null = null;
  let attempt = 0;

  // Keyed on the loader as well as the attempt: switching to another section reuses this
  // instance with a different `load`, and must not keep showing the previous panel.
  $: void start(load, attempt);

  async function start(loader: ComponentLoader, _attempt: number) {
    failedLoader = null;
    if (loaded?.loader === loader) return;
    loaded = null;
    try {
      const module = await loader();
      if (loader === load) loaded = { loader, component: module.default };
    } catch (err) {
      console.error('Failed to load settings section:', err);
      if (loader === load) failedLoader = loader;
    }
  }

  $: ready = loaded !== null && loaded.loader === load;
  $: failed = failedLoader === load;
</script>

{#if ready && loaded}
  <slot Component={loaded.component} />
{:else if failed}
  <div class="section-load-error" role="alert" data-testid="settings-section-error">
    <p>{$t('settings.modal.sectionLoadFailed')}</p>
    <button type="button" class="btn btn-secondary" on:click={() => (attempt += 1)}>
      {$t('settings.modal.sectionRetry')}
    </button>
  </div>
{:else}
  <div
    class="section-loading"
    role="status"
    aria-live="polite"
    aria-label={$t('settings.modal.sectionLoading')}
    data-testid="settings-section-loading"
  >
    <SettingsSkeleton {variant} count={lines} />
  </div>
{/if}

<style>
  .section-loading {
    padding: 0.5rem 0;
  }

  .section-load-error {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--background-color);
    color: var(--text-color);
  }

  .section-load-error p {
    margin: 0;
    font-size: 0.875rem;
    color: var(--text-secondary);
  }
</style>
