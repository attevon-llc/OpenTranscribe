<!--
  SpeakerDetectionProviderCard.svelte — who detects speakers, and what credential that needs.

  One card in the shape of the transcription provider UX: pick the provider, read what it means,
  see which credential it uses. Only the pyannote.ai cloud service has a credential of its own
  (it is not the local PyAnnote fallback engine), so only that option reveals the key form;
  Automatic points at the transcription provider's key instead of asking for it again here.

  Issue #1227: the card also says, before anything is saved, what the CHOICE means for the
  current transcription model (text only, no timing, lite) and where it would run. Those
  notices come from the pipeline view's facts about the transcription step; the server
  re-checks on save and answers 409 with a stable code, which the parent shows.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { PipelineView } from '$lib/api/pipeline';
  import type { DiarizationSource } from '$lib/api/transcriptionSettings';
  import {
    optionWithFlags,
    type DiarizerChoice,
    type SavedProviderRow
  } from '$lib/settings/asrCapabilities';
  import { PIPELINE_ISSUE_KEYS } from '$lib/settings/pipelineKeys';
  import { fixTargets, isPipelineIssueCode } from '$lib/settings/pipelineIssues';
  import {
    billingNoteKey,
    selectionNotices,
    selectionWhere,
    speakerSourceOptions,
    transcriptionFacts
  } from '$lib/settings/speakerSources';
  import type { SettingsSection } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';
  import Chip from '../ui/Chip.svelte';
  import CapabilityBadges from './CapabilityBadges.svelte';
  import SettingsCard from './transcription/SettingsCard.svelte';
  import PyannoteCredentialForm from './PyannoteCredentialForm.svelte';
  import IssueRow from './IssueRow.svelte';

  /** The user's current (possibly unsaved) choice. */
  export let diarizationSource: DiarizationSource = 'provider';
  /** The saved ASR config named as the speaker detector; used only for `asr_config`. */
  export let diarizationAsrConfigUuid: string | null = null;
  /** Saved configs that may be chosen as the detector (they return speakers). */
  export let choices: DiarizerChoice[] = [];
  /** Every saved config with what it returns, so the card can say which ones qualify. */
  export let saved: SavedProviderRow[] = [];
  /** The saved pipeline, for the transcription model's facts and the deployment mode. */
  export let pipeline: PipelineView | null = null;
  /** The deployment manages the source; the select is read-only. */
  export let locked = false;
  /** Disable the select and the key form, e.g. while the parent form is saving. */
  export let disabled = false;
  /** Whether the deployment lets users bring their own provider keys. */
  export let keyEnabled = true;

  const dispatch = createEventDispatcher<{
    navigate: SettingsSection;
    deleted: { sourceReverted: boolean };
  }>();

  $: mode = pipeline?.deployment_mode ?? 'full';
  $: options = speakerSourceOptions({
    mode,
    current: diarizationSource,
    asrConfigEnabled: keyEnabled
  });
  $: selected = options.find((option) => option.value === diarizationSource);
  $: chosen = choices.find((choice) => choice.uuid === diarizationAsrConfigUuid);
  $: notices = selectionNotices(
    diarizationSource,
    transcriptionFacts(pipeline),
    mode,
    chosen
  ).map(
    (notice) => ({
      ...notice,
      messageKey: isPipelineIssueCode(notice.code) ? PIPELINE_ISSUE_KEYS[notice.code] : null,
      // This card is the Speaker Detection tab: only offer the jump to the OTHER tab.
      sections: fixTargets({ code: notice.code, step: 'diarization' }).filter(
        (section) => section !== 'speaker-identification'
      )
    })
  );
  $: billingNote = billingNoteKey(diarizationSource, pipeline);
  $: where = selectionWhere(diarizationSource, pipeline, chosen);

  // Choosing another provider must name one; start on the first that can serve.
  $: if (diarizationSource === 'asr_config' && !diarizationAsrConfigUuid && choices.length > 0) {
    diarizationAsrConfigUuid = choices[0].uuid;
  }
</script>

<SettingsCard
  title={$t('settings.speakerIdentification.detectionHeading')}
  description={$t('settings.speakerIdentification.detectionDesc')}
>
  <div class="form-group">
    <label for="diarization-source" class="form-label">
      {$t('settings.speakerIdentification.source.label')}
    </label>
    <select
      id="diarization-source"
      class="form-select"
      bind:value={diarizationSource}
      disabled={disabled || locked}
    >
      {#each options as source (source.value)}
        <option value={source.value} disabled={source.disabled}>{$t(source.labelKey)}</option>
      {/each}
    </select>
    <p class="field-desc">{selected ? $t(selected.descKey) : ''}</p>

    {#if locked}
      <p class="field-desc" data-testid="source-locked-note">
        {$t(PIPELINE_ISSUE_KEYS.source_locked)}
      </p>
    {:else}
      {#if where}
        <p class="where-line" data-testid="detection-where">
          <Chip variant={where.where === 'cloud' ? 'info' : 'default'}>
            {where.where === 'cloud'
              ? $t('settings.pipeline.where.cloud', { vendor: where.vendor ?? '' })
              : $t('settings.pipeline.where.server')}
          </Chip>
        </p>
      {/if}

      {#if notices.length > 0}
        <ul class="notices">
          {#each notices as notice (notice.code)}
            <IssueRow
              code={notice.code}
              severity={notice.severity}
              messageKey={notice.messageKey}
              sections={notice.sections}
              rowTestId="selection-notice"
              linkTestId="selection-notice-link"
              on:navigate={(e) => dispatch('navigate', e.detail)}
            />
          {/each}
        </ul>
      {/if}

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
          {:else if diarizationSource === 'asr_config'}
            {$t('settings.speakerIdentification.credential.asrConfig')}
          {:else}
            {$t('settings.speakerIdentification.source.pyannoteHint')}
          {/if}
        </p>
      {/if}

      {#if diarizationSource === 'asr_config'}
        {#if choices.length > 0}
          <label for="diarization-asr-config" class="form-label sub-label">
            {$t('settings.speakerIdentification.source.asrConfigSelect')}
          </label>
          <select
            id="diarization-asr-config"
            class="form-select"
            bind:value={diarizationAsrConfigUuid}
            disabled={disabled}
          >
            {#each choices as choice (choice.uuid)}
              <option value={choice.uuid}>
                {optionWithFlags(
                  `${choice.name} (${choice.providerLabel}, ${choice.modelName})`,
                  choice.timing ? { returnsSpeakers: choice.returnsSpeakers, timing: choice.timing } : null,
                  $t
                )}
              </option>
            {/each}
          </select>
          {#if chosen}
            <p class="chosen-flags" data-testid="asr-config-flags">
              <CapabilityBadges returnsSpeakers={chosen.returnsSpeakers} timing={chosen.timing} />
            </p>
          {/if}
        {:else}
          <p class="field-desc">
            {$t('settings.speakerIdentification.source.asrConfigNone')}
            <button
              type="button"
              class="link-button"
              data-testid="asr-config-add-link"
              on:click={() => dispatch('navigate', 'asr-provider')}
            >
              {$t('settings.pipeline.goTo.asrProvider')}
            </button>
          </p>
        {/if}
        {#if saved.length > 0}
          <p class="field-desc saved-heading">{$t('settings.speakerIdentification.source.asrConfigSaved')}</p>
          <ul class="saved-list" data-testid="asr-config-saved">
            {#each saved as row (row.uuid)}
              <li data-testid="asr-config-saved-row" data-qualifies={row.returnsSpeakers && !row.transcribing}>
                <span class="saved-name">{row.name}</span>
                <span class="saved-meta">{row.providerLabel}, {row.modelName}</span>
                <CapabilityBadges returnsSpeakers={row.returnsSpeakers} timing={row.timing} />
                {#if row.transcribing}
                  <span class="saved-meta">{$t('settings.speakerIdentification.source.asrConfigTranscribing')}</span>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      {/if}

      {#if billingNote}
        <p class="field-desc" data-testid="detection-billing-note">{$t(billingNote)}</p>
      {/if}

      {#if diarizationSource === 'pyannote' && keyEnabled}
        <PyannoteCredentialForm
          {diarizationSource}
          {disabled}
          on:deleted={(e) => dispatch('deleted', e.detail)}
        />
      {/if}
    {/if}
  </div>
</SettingsCard>

<style>
  .where-line {
    margin: 0.5rem 0 0;
  }

  .notices {
    list-style: none;
    margin: 0.5rem 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .sub-label {
    display: block;
    margin-top: 0.75rem;
  }

  .chosen-flags {
    margin: 0.5rem 0 0;
  }

  .saved-heading {
    margin-top: 0.75rem;
  }

  .saved-list {
    list-style: none;
    margin: 0.25rem 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .saved-list li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.25rem 0.5rem;
    font-size: 0.8125rem;
  }

  .saved-list li[data-qualifies='false'] .saved-name {
    color: var(--text-secondary);
  }

  .saved-name {
    font-weight: 500;
    color: var(--text-primary);
  }

  .saved-meta {
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

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
