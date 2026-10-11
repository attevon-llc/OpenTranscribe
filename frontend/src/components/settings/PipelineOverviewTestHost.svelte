<!-- Test host: Svelte 5 cannot observe a legacy dispatched event except through a consumer's markup. -->
<script lang="ts">
  import type { SettingsSection } from '$stores/settingsModalStore';
  import PipelineOverview from './PipelineOverview.svelte';

  export let onNavigate: (section: SettingsSection) => void = () => {};
</script>

<PipelineOverview on:navigate={(e) => onNavigate(e.detail)} />
