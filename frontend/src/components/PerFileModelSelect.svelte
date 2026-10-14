<!--
  PerFileModelSelect.svelte — the transcription model choice of one upload or reprocess (#1243).

  A native select cannot draw pills, so every option carries its flags in its own label
  ("..., Speakers included, Word timing") and the selected option repeats them as the shared
  `CapabilityBadges` with what they mean for speaker detection. The options and their flags
  come from `$lib/settings/perFileModel` (the ASR catalog and the user's saved provider), so
  a cloud provider is named as what "my setting" runs instead of the server's own model.
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { optionWithFlags } from '$lib/settings/asrCapabilities';
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

<select {id} bind:value on:change={() => dispatch('change')} class="model-select">
  {#each options as option (option.kind)}
    <option value={option.value}>{optionWithFlags(optionName(option, $t), option.capability, $t)}</option>
  {/each}
</select>
{#if selected?.capability}
  <div class="model-flags" data-testid="per-file-model-flags">
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
    width: 100%;
    padding: 0.4375rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--input-bg, var(--surface-color));
    color: var(--text-primary);
    font-size: 0.875rem;
    cursor: pointer;
  }

  .model-select:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
  }

  .model-flags {
    margin-top: 0.375rem;
  }

  @media (max-width: 640px) {
    .model-select {
      min-height: 44px;
      font-size: 1rem;
    }
  }
</style>
