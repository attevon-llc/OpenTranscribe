<!--
  PerFileModelSelect.svelte — the transcription model choice of one upload or reprocess (#1243).

  A native option cannot wrap, so a flag-laden label was cut at the select's edge in en and ar.
  Each option is just the model name; the selected option's flags show as the shared
  `CapabilityBadges` under the select, linked to it with aria-describedby. The options and their flags
  come from `$lib/settings/perFileModel` (the ASR catalog and the user's saved provider), so
  a cloud provider is named as what "my setting" runs instead of the server's own model.
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import {
    loadPerFileFacts,
    optionName,
    perFileOptions,
    type PerFileFacts,
  } from '$lib/settings/perFileModel';
  import { t } from '$stores/locale';
  import CapabilityBadges from '$components/settings/CapabilityBadges.svelte';

  /** `whisper_model` to send: null keeps the user's transcription setting. Bindable. */
  export let value: string | null = null;
  /** This server's own model, shown when "my setting" is the built-in one. */
  export let adminModel = 'large-v3-turbo';
  export let id = 'whisper-model-select';

  const dispatch = createEventDispatcher<{ change: void }>();

  let facts: PerFileFacts | null = null;
  onMount(() => {
    void loadPerFileFacts().then((loaded) => (facts = loaded));
  });

  $: options = perFileOptions(facts, adminModel);
  // A stored Fast choice must not survive into a deployment that has no Fast option.
  $: if (value !== null && !options.some((option) => option.value === value)) value = null;
  $: selected = options.find((option) => option.value === value) ?? options[0];
  $: lite = facts?.status.deployment_mode === 'lite';
</script>

<select
  {id}
  bind:value
  on:change={() => dispatch('change')}
  class="model-select"
  aria-describedby={selected?.capability ? `${id}-flags` : undefined}
>
  {#each options as option (option.kind)}
    <option value={option.value}>{optionName(option, $t)}</option>
  {/each}
</select>
{#if selected?.capability}
  <div class="model-flags" id="{id}-flags" data-testid="per-file-model-flags">
    <CapabilityBadges
      returnsSpeakers={selected.capability.returnsSpeakers}
      timing={selected.capability.timing}
      local={selected.local}
      {lite}
      explain
    />
  </div>
{/if}

<style>
  .model-select {
    inline-size: 100%;
    max-inline-size: 100%;
    min-inline-size: 0;
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-md);
    cursor: pointer;
  }

  .model-flags {
    margin-top: var(--space-1-5);
  }

  @media (max-width: 640px) {
    .model-select {
      min-height: 44px;
      font-size: var(--font-size-base);
    }
  }
</style>
