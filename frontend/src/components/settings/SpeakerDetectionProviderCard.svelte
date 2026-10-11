<!--
  SpeakerDetectionProviderCard.svelte — who detects speakers, and what credential that needs.

  One card in the shape of the transcription provider UX: pick the provider, read what it means,
  see which credential it uses. Only the pyannote.ai cloud service has a credential of its own
  (it is not the local PyAnnote fallback engine), so only that option reveals the key form;
  Automatic points at the transcription provider's key instead of asking for it again here.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { DiarizationSource } from '$lib/api/transcriptionSettings';
  import type { SettingsSection } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';
  import SettingsCard from './transcription/SettingsCard.svelte';
  import PyannoteCredentialForm from './PyannoteCredentialForm.svelte';

  /** The user's current (possibly unsaved) choice. */
  export let diarizationSource: DiarizationSource = 'provider';
  /** Disable the select and the key form, e.g. while the parent form is saving. */
  export let disabled = false;
  /** Whether the deployment lets users bring their own provider keys. */
  export let keyEnabled = true;

  const dispatch = createEventDispatcher<{
    navigate: SettingsSection;
    deleted: { sourceReverted: boolean };
  }>();

  const sourceOptions: { value: DiarizationSource; label: string; desc: string }[] = [
    {
      value: 'provider',
      label: 'settings.speakerIdentification.source.provider',
      desc: 'settings.speakerIdentification.source.providerDesc'
    },
    {
      value: 'local',
      label: 'settings.speakerIdentification.source.local',
      desc: 'settings.speakerIdentification.source.localDesc'
    },
    {
      value: 'pyannote',
      label: 'settings.speakerIdentification.source.pyannote',
      desc: 'settings.speakerIdentification.source.pyannoteDesc'
    },
    {
      value: 'off',
      label: 'settings.speakerIdentification.source.off',
      desc: 'settings.speakerIdentification.source.offDesc'
    }
  ];
</script>

<SettingsCard
  title={$t('settings.speakerIdentification.detectionHeading')}
  description={$t('settings.speakerIdentification.detectionDesc')}
>
  <div class="form-group">
    <label for="diarization-source" class="form-label">
      {$t('settings.speakerIdentification.source.label')}
    </label>
    <select id="diarization-source" class="form-select" bind:value={diarizationSource}>
      {#each sourceOptions as source (source.value)}
        <option value={source.value}>{$t(source.label)}</option>
      {/each}
    </select>
    <p class="field-desc">
      {$t(sourceOptions.find((source) => source.value === diarizationSource)?.desc ?? '')}
    </p>

    {#if diarizationSource === 'off'}
      <p class="field-warning">{$t('settings.speakerIdentification.source.offWarning')}</p>
    {:else}
      <p
        class="field-desc"
        class:hint-italic={diarizationSource === 'pyannote'}
        data-testid="detection-credential-line"
        data-source={diarizationSource}
      >
        {#if diarizationSource === 'provider'}
          {$t('settings.speakerIdentification.credential.provider')}
          <button
            type="button"
            class="link-button"
            on:click={() => dispatch('navigate', 'asr-provider')}
          >
            {$t('settings.speakerIdentification.credential.providerLink', {
              section: $t('settings.transcription.title'),
              tab: $t('settings.transcription.tabs.provider')
            })}
          </button>
        {:else if diarizationSource === 'local'}
          {$t('settings.speakerIdentification.credential.local')}
        {:else}
          {$t('settings.speakerIdentification.source.pyannoteHint')}
        {/if}
      </p>
    {/if}

    {#if diarizationSource === 'pyannote' && keyEnabled}
      <PyannoteCredentialForm
        {diarizationSource}
        {disabled}
        on:deleted={(e) => dispatch('deleted', e.detail)}
      />
    {/if}
  </div>
</SettingsCard>

<style>
  .link-button {
    display: inline;
    margin-inline-start: 0.25rem;
    padding: 0;
    border: none;
    border-radius: 0;
    box-shadow: none;
    background: none;
    color: var(--primary-on-surface);
    font: inherit;
    text-decoration: underline;
    cursor: pointer;
  }

  .link-button:hover,
  .link-button:focus {
    background: none;
    box-shadow: none;
    transform: none;
  }

  .link-button:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
</style>
