<!--
  SpeakerIdentificationSection.svelte — the "Speaker Identification" Settings entry: who spoke when.

  Four tabs (speaker detection, voice attributes, the deployment-wide speaker engine, and
  library maintenance), chosen by `speakerIdentificationTabs` from role and capabilities.
  Nothing here changes the transcribed words; that is TranscriptionSection.

  The engine and maintenance tabs are super_admin. An admin sees them locked (disabled, with a
  tooltip naming the tier) rather than missing; a plain user never sees them. Panels stay
  mounted once visited so unsaved edits survive a tab switch, and this shell owns the section's
  dirty flag while the forms only dispatch `change`.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { settingsModalStore, type SettingsSection } from '$stores/settingsModalStore';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import SpeakerDetectionSettings from '$components/settings/SpeakerDetectionSettings.svelte';
  import SpeakerAttributeSettings from '$components/settings/SpeakerAttributeSettings.svelte';
  import SpeakerAttributeBulkPanel from '$components/settings/SpeakerAttributeBulkPanel.svelte';
  import EngineSettings from '$components/settings/EngineSettings.svelte';
  import {
    speakerIdentificationTabs,
    resolveSpeakerIdTab,
    type SpeakerIdTabId,
  } from '$lib/settings/speakerIdentificationTabs';

  /** Deep-link support: the legacy `engine-settings` / `speaker-attributes` ids open their tab. */
  export let initialTab: SpeakerIdTabId = 'spk-detection';
  export let isAdmin = false;
  export let isSuperAdmin = false;
  export let prefsCap = true;
  export let engineCap = true;
  export let migrationCap = true;
  /** Whether the Speaker Embedding System section exists, so Maintenance can link to it. */
  export let embeddingCap = true;

  const dispatch = createEventDispatcher<{ navigate: SettingsSection }>();

  $: specs = speakerIdentificationTabs({ isAdmin, isSuperAdmin, prefsCap, engineCap, migrationCap });

  // string, not the id union: <Tabs bind:activeId> is string-typed.
  let activeId: string = initialTab;
  let seenInitial = initialTab;
  $: if (initialTab !== seenInitial) {
    seenInitial = initialTab;
    activeId = initialTab;
  }
  // A tab the role cannot open (or that vanished on demotion) is never left active.
  $: {
    const next = resolveSpeakerIdTab(activeId as SpeakerIdTabId, specs);
    if (next && next !== activeId) activeId = next;
  }

  const visited: Record<string, boolean> = {};
  $: if (activeId) visited[activeId] = true;

  let dirty = { detection: false, attributes: false, engine: false };
  $: settingsModalStore.setDirty('speaker-identification', Object.values(dirty).some(Boolean));

  const labelKey: Record<SpeakerIdTabId, string> = {
    'spk-detection': 'settings.speakerIdentification.tabs.detection',
    'spk-attributes': 'settings.speakerIdentification.tabs.attributes',
    'spk-engine': 'settings.speakerIdentification.tabs.engine',
    'spk-maintenance': 'settings.speakerIdentification.tabs.maintenance',
  };

  $: tabItems = specs.map<TabItem>((spec) => {
    const label = $t(labelKey[spec.id]);
    if (spec.locked) {
      return {
        id: spec.id,
        label,
        disabled: true,
        badge: '🔒',
        title: $t('settings.nav.requiresSuperAdmin'),
      };
    }
    const hasEdits =
      (spec.id === 'spk-detection' && dirty.detection) ||
      (spec.id === 'spk-attributes' && dirty.attributes) ||
      (spec.id === 'spk-engine' && dirty.engine);
    return { id: spec.id, label, ...(hasEdits ? { badge: '●' } : {}) };
  });

  $: activeSpec = specs.find((s) => s.id === activeId);
  const open = (id: SpeakerIdTabId) => specs.some((s) => s.id === id && !s.locked);
</script>

<div class="speaker-identification-section" data-testid="speaker-identification-section">
  {#if tabItems.length > 1}
    <div class="tab-strip">
      <Tabs
        tabs={tabItems}
        bind:activeId
        ariaLabel={$t('settings.speakerIdentification.tabs.ariaLabel')}
      />
    </div>
  {/if}

  {#if activeSpec?.locked}
    <EmptyState
      icon="🔒"
      title={$t('settings.permission.superAdminTitle')}
      description={$t('settings.permission.superAdminMessage')}
    />
  {/if}

  {#if open('spk-detection')}
    <div
      id="tabpanel-spk-detection"
      role="tabpanel"
      aria-labelledby="tab-spk-detection"
      hidden={activeId !== 'spk-detection'}
    >
      {#if visited['spk-detection']}
        <SpeakerDetectionSettings on:change={(e) => (dirty.detection = e.detail.hasChanges)} />
      {/if}
    </div>
  {/if}

  {#if open('spk-attributes')}
    <div
      id="tabpanel-spk-attributes"
      role="tabpanel"
      aria-labelledby="tab-spk-attributes"
      hidden={activeId !== 'spk-attributes'}
    >
      {#if visited['spk-attributes']}
        <SpeakerAttributeSettings on:change={(e) => (dirty.attributes = e.detail.hasChanges)} />
      {/if}
    </div>
  {/if}

  {#if open('spk-engine')}
    <div
      id="tabpanel-spk-engine"
      role="tabpanel"
      aria-labelledby="tab-spk-engine"
      hidden={activeId !== 'spk-engine'}
    >
      {#if visited['spk-engine']}
        <p class="tab-description">{$t('settings.engineSettings.description')}</p>
        <EngineSettings on:change={(e) => (dirty.engine = e.detail.hasChanges)} />
      {/if}
    </div>
  {/if}

  {#if open('spk-maintenance')}
    <div
      id="tabpanel-spk-maintenance"
      role="tabpanel"
      aria-labelledby="tab-spk-maintenance"
      hidden={activeId !== 'spk-maintenance'}
    >
      {#if visited['spk-maintenance']}
        <h4 class="maintenance-heading">{$t('settings.speakerIdentification.maintenance.heading')}</h4>
        <p class="tab-description">{$t('settings.speakerIdentification.maintenance.desc')}</p>
        <SpeakerAttributeBulkPanel />
        {#if embeddingCap}
          <div class="embedding-link" data-testid="embedding-link-card">
            <div class="embedding-link-text">
              <strong>{$t('settings.speakerIdentification.maintenance.embeddingLinkTitle')}</strong>
              <p>{$t('settings.speakerIdentification.maintenance.embeddingLinkDesc')}</p>
            </div>
            <button
              type="button"
              class="btn btn-secondary"
              on:click={() => dispatch('navigate', 'embedding-migration')}
            >
              {$t('settings.speakerIdentification.maintenance.embeddingLinkButton')}
            </button>
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</div>

<style>
  .tab-strip {
    margin-bottom: 1.25rem;
  }

  .tab-description {
    margin: 0 0 1rem;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .maintenance-heading {
    margin: 0 0 0.25rem;
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .embedding-link {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-top: 1.5rem;
    padding: 1rem 1.25rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .embedding-link-text p {
    margin: 0.25rem 0 0;
    color: var(--text-secondary);
    font-size: 0.85rem;
  }

  @media (max-width: 600px) {
    .embedding-link {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
