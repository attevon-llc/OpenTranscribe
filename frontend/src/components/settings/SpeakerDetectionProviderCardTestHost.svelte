<!-- Test host: Svelte 5 cannot observe a legacy dispatched event except through a consumer's markup. -->
<script lang="ts">
  import type { DiarizationSource } from '$lib/api/transcriptionSettings';
  import type { PipelineView } from '$lib/api/pipeline';
  import type { DiarizerChoice } from '$lib/settings/asrCapabilities';
  import type { SettingsSection } from '$stores/settingsModalStore';
  import SpeakerDetectionProviderCard from './SpeakerDetectionProviderCard.svelte';

  export let diarizationSource: DiarizationSource = 'provider';
  export let diarizationAsrConfigUuid: string | null = null;
  export let pipeline: PipelineView | null = null;
  export let choices: DiarizerChoice[] = [];
  export let locked = false;
  export let keyEnabled = true;
  export let onNavigate: (section: SettingsSection) => void = () => {};
</script>

<SpeakerDetectionProviderCard
  bind:diarizationSource
  bind:diarizationAsrConfigUuid
  {pipeline}
  {choices}
  {locked}
  {keyEnabled}
  on:navigate={(e) => onNavigate(e.detail)}
/>
<output data-testid="host-source">{diarizationSource}</output>
<output data-testid="host-uuid">{diarizationAsrConfigUuid ?? ''}</output>
