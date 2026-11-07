<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import { getVadDefaults, updateVadDefaults, type VadDefaults } from '$lib/api/transcriptionDefaults';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let loading = true;
  let saving = false;
  let form: VadDefaults | null = null;
  let saved: VadDefaults | null = null;

  $: changed = form !== null && saved !== null && JSON.stringify(form) !== JSON.stringify(saved);

  onMount(async () => {
    try {
      saved = await getVadDefaults();
      form = { ...saved };
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.transcriptionDefaults.loadFailed')));
    } finally {
      loading = false;
    }
  });

  async function save() {
    if (!form) return;
    saving = true;
    try {
      saved = await updateVadDefaults(form);
      form = { ...saved };
      toastStore.success($t('settings.transcriptionDefaults.saved'));
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.transcriptionDefaults.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<SettingsCard
  title={$t('settings.transcriptionDefaults.title')}
  description={$t('settings.transcriptionDefaults.desc')}
>
  {#if loading}
    <SettingsSkeleton variant="form" count={2} label={$t('common.loading')} />
  {:else if form}
    <div class="defaults-form">
      <label class="field">
        <span>{$t('settings.transcription.vadThreshold')}</span>
        <input
          type="number"
          id="default-vad-threshold"
          class="form-input"
          min="0.1"
          max="0.95"
          step="0.05"
          bind:value={form.vad_threshold}
        />
      </label>
      <label class="field">
        <span>{$t('settings.transcription.vadMinSilence')}</span>
        <input
          type="number"
          id="default-vad-min-silence"
          class="form-input"
          min="100"
          max="5000"
          step="50"
          bind:value={form.vad_min_silence_ms}
        />
      </label>
      <label class="field">
        <span>{$t('settings.transcription.vadMinSpeech')}</span>
        <input
          type="number"
          id="default-vad-min-speech"
          class="form-input"
          min="50"
          max="5000"
          step="50"
          bind:value={form.vad_min_speech_ms}
        />
      </label>
      <label class="field">
        <span>{$t('settings.transcription.vadSpeechPad')}</span>
        <input
          type="number"
          id="default-vad-speech-pad"
          class="form-input"
          min="0"
          max="2000"
          step="50"
          bind:value={form.vad_speech_pad_ms}
        />
      </label>
      <div class="actions">
        <button type="button" class="btn btn-primary" on:click={save} disabled={saving || !changed}>
          {$t('settings.transcriptionDefaults.save')}
        </button>
      </div>
    </div>
  {/if}
</SettingsCard>

<style>
  .defaults-form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
    gap: var(--space-3);
    align-items: end;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--font-size-md);
    color: var(--text-color);
  }
  .form-input {
    padding: var(--space-1-5) var(--space-2);
  }
  .actions {
    grid-column: 1 / -1;
    display: flex;
    justify-content: flex-end;
  }
</style>
