<script lang="ts">
  import { settingsModalStore } from '$stores/settingsModalStore';
  import TranscriptionLanguageSettings from './TranscriptionLanguageSettings.svelte';
  import TranscriptionAccuracySettings from './TranscriptionAccuracySettings.svelte';
  import SpeakerDetectionSettings from './SpeakerDetectionSettings.svelte';

  // Stand-in shell until the tabbed sections replace this panel: each form owns its own
  // save/reset, and the modal's single dirty flag is the union of the three.
  const dirty = { language: false, accuracy: false, speakers: false };
  $: settingsModalStore.setDirty('transcription', Object.values(dirty).some(Boolean));
</script>

<div class="transcription-settings">
  <TranscriptionLanguageSettings
    on:change={(e) => (dirty.language = e.detail.hasChanges)}
  />
  <TranscriptionAccuracySettings
    on:change={(e) => (dirty.accuracy = e.detail.hasChanges)}
  />
  <SpeakerDetectionSettings on:change={(e) => (dirty.speakers = e.detail.hasChanges)} />
</div>

<style>
  .transcription-settings {
    display: flex;
    flex-direction: column;
    gap: 2rem;
    padding: 0.5rem 0;
  }
</style>
