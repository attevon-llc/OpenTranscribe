<!--
  TranscriptionSection.svelte — the "Transcription" Settings entry: turning speech into words.

  Four tabs (language, provider and model, vocabulary, accuracy and cleanup), chosen by
  `transcriptionTabs` from the deployment's capabilities. Who spoke when is a different job
  and lives in SpeakerIdentificationSection.

  Panels stay mounted once visited and are only hidden when inactive: the language and
  accuracy forms hold unsaved edits and report them upward, so unmounting on a tab switch
  would silently discard them. This shell owns the section's dirty flag; the forms only
  dispatch `change`.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import TranscriptionLanguageSettings from '$components/settings/TranscriptionLanguageSettings.svelte';
  import TranscriptionAccuracySettings from '$components/settings/TranscriptionAccuracySettings.svelte';
  import ASRSettings from '$components/settings/ASRSettings.svelte';
  import CustomVocabularySettings from '$components/settings/CustomVocabularySettings.svelte';
  import {
    transcriptionTabs,
    resolveTranscriptionTab,
    type TranscriptionTabId,
  } from '$lib/settings/transcriptionTabs';

  /** Deep-link support: the legacy `asr-provider` / `custom-vocabulary` ids open their tab. */
  export let initialTab: TranscriptionTabId = 'tx-language';
  export let isAdmin = false;
  export let isSuperAdmin = false;
  export let prefsCap = true;
  export let asrCap = true;
  export let vocabCap = true;

  $: specs = transcriptionTabs({ prefsCap, asrCap, vocabCap });

  // string, not the id union: <Tabs bind:activeId> is string-typed.
  let activeId: string = initialTab;
  let seenInitial = initialTab;
  $: if (initialTab !== seenInitial) {
    seenInitial = initialTab;
    activeId = initialTab;
  }
  // A tab the deployment lacks (or that vanished on a capability change) is never left active.
  $: {
    const next = resolveTranscriptionTab(activeId as TranscriptionTabId, specs);
    if (next && next !== activeId) activeId = next;
  }

  const visited: Record<string, boolean> = {};
  $: if (activeId) visited[activeId] = true;

  let dirty = { language: false, accuracy: false };
  $: settingsModalStore.setDirty('transcription', dirty.language || dirty.accuracy);

  const labelKey: Record<TranscriptionTabId, string> = {
    'tx-language': 'settings.transcription.tabs.language',
    'tx-provider': 'settings.transcription.tabs.provider',
    'tx-vocabulary': 'settings.transcription.tabs.vocabulary',
    'tx-accuracy': 'settings.transcription.tabs.accuracy',
  };

  $: tabItems = specs.map<TabItem>((spec) => {
    const hasEdits =
      (spec.id === 'tx-language' && dirty.language) ||
      (spec.id === 'tx-accuracy' && dirty.accuracy);
    return { id: spec.id, label: $t(labelKey[spec.id]), ...(hasEdits ? { badge: '●' } : {}) };
  });

  const has = (id: TranscriptionTabId) => specs.some((s) => s.id === id);
</script>

<div class="transcription-section" data-testid="transcription-section">
  {#if tabItems.length > 1}
    <div class="tab-strip">
      <Tabs tabs={tabItems} bind:activeId ariaLabel={$t('settings.transcription.tabs.ariaLabel')} />
    </div>
  {/if}

  {#if has('tx-language')}
    <div
      id="tabpanel-tx-language"
      role="tabpanel"
      aria-labelledby="tab-tx-language"
      hidden={activeId !== 'tx-language'}
    >
      {#if visited['tx-language']}
        <TranscriptionLanguageSettings on:change={(e) => (dirty.language = e.detail.hasChanges)} />
      {/if}
    </div>
  {/if}

  {#if has('tx-provider')}
    <div
      id="tabpanel-tx-provider"
      role="tabpanel"
      aria-labelledby="tab-tx-provider"
      hidden={activeId !== 'tx-provider'}
    >
      {#if visited['tx-provider']}
        <p class="tab-description">{$t('settings.asrProvider.description')}</p>
        <ASRSettings {isAdmin} {isSuperAdmin} />
      {/if}
    </div>
  {/if}

  {#if has('tx-vocabulary')}
    <div
      id="tabpanel-tx-vocabulary"
      role="tabpanel"
      aria-labelledby="tab-tx-vocabulary"
      hidden={activeId !== 'tx-vocabulary'}
    >
      {#if visited['tx-vocabulary']}<CustomVocabularySettings />{/if}
    </div>
  {/if}

  {#if has('tx-accuracy')}
    <div
      id="tabpanel-tx-accuracy"
      role="tabpanel"
      aria-labelledby="tab-tx-accuracy"
      hidden={activeId !== 'tx-accuracy'}
    >
      {#if visited['tx-accuracy']}
        <TranscriptionAccuracySettings on:change={(e) => (dirty.accuracy = e.detail.hasChanges)} />
      {/if}
    </div>
  {/if}
</div>

<style>
  .tab-strip {
    margin-bottom: 1.25rem;
  }

  .tab-description {
    max-width: 800px;
    margin: 0 0 1rem;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }
</style>
