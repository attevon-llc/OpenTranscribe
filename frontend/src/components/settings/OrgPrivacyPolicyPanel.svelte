<!--
  OrgPrivacyPolicyPanel.svelte — an organization administrator's privacy policy (issue #1280).

  Two restrict-only switches for everyone in the organization: matching speakers by voice, and
  inferring gender from voice. A layer above the organization (the deployment or the platform)
  can already have a feature off; this panel then shows the switch disabled and says who turned
  it off. Switching a feature off hides data; the separate purge deletes it and is only offered
  once the switch is off. The server decides every outcome, this renders it.

  The organization comes from the session on the server; there is no organization parameter.
  The redaction floor (#1279) is shown only when the server returns one, which it does only
  while the `redaction.policy` capability is on; it is the same server gate the PATCH enforces.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import RangeSlider from '$components/ui/RangeSlider.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import PrivacySwitchCard from '$components/privacy/PrivacySwitchCard.svelte';
  import PurgeConfirmDialog from '$components/privacy/PurgeConfirmDialog.svelte';
  import {
    OrgAdminApi,
    type FloorCategory,
    type OrgPolicy,
    type OrgPolicyChanges,
  } from '$lib/api/orgAdmin';
  import type { PrivacyDecision, PurgeTarget } from '$lib/api/privacy';
  import { getErrorStatus } from '$lib/utils/apiError';
  import {
    LOCKED_BY_KEYS,
    isLockedAboveOrganization,
    privacyErrorKey,
  } from '$lib/settings/privacyPolicy';
  import { DEFAULT_REDACTION_SETTINGS } from '$lib/api/redactionSettings';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  type Feature = 'voiceprints' | 'gender';
  type FloorBoolean = 'force_export_redacted' | 'force_redact_before_llm';

  const FLOOR_CATEGORIES: { id: FloorCategory; label: string }[] = [
    { id: 'pii', label: 'settings.redactionPolicy.forcePii' },
    { id: 'toxicity', label: 'settings.redactionPolicy.forceToxicity' },
    { id: 'profanity', label: 'settings.redactionPolicy.forceProfanity' },
  ];
  // The same vocabulary the API validates forced_pii_entities against.
  const PII_ENTITIES = DEFAULT_REDACTION_SETTINGS.pii_entities;
  const DEFAULT_THRESHOLD = DEFAULT_REDACTION_SETTINGS.toxicity_threshold;

  const FLOOR_BOOLEANS: { id: FloorBoolean; label: string; help: string }[] = [
    {
      id: 'force_export_redacted',
      label: 'settings.redactionPolicy.forceExportRedacted',
      help: 'settings.redactionPolicy.forceExportRedactedHelp',
    },
    {
      id: 'force_redact_before_llm',
      label: 'settings.redactionPolicy.forceRedactBeforeLlm',
      help: 'settings.redactionPolicy.forceRedactBeforeLlmHelp',
    },
  ];

  let policy: OrgPolicy | null = null;
  let loading = true;
  let unavailable = false;
  let loadFailed = false;
  let busy: Feature | 'floor' | null = null;
  let purging: PurgeTarget[] = [];

  const FIELD = {
    voiceprints: 'voiceprint_matching_enabled',
    gender: 'gender_inference_enabled',
  } as const satisfies Record<Feature, keyof OrgPolicyChanges>;

  function decisionOf(feature: Feature): PrivacyDecision | null {
    if (!policy) return null;
    return feature === 'voiceprints'
      ? policy.effective.voiceprint_matching
      : policy.effective.gender_inference;
  }

  function lockedLayer(feature: Feature) {
    const layer = decisionOf(feature)?.locked_by ?? null;
    return isLockedAboveOrganization(layer) ? layer : null;
  }

  function lockedReason(feature: Feature): string {
    const layer = lockedLayer(feature);
    return layer ? $t(LOCKED_BY_KEYS[layer]) : '';
  }

  async function load() {
    loading = true;
    unavailable = false;
    loadFailed = false;
    try {
      policy = await OrgAdminApi.getPolicy();
    } catch (error) {
      console.error('Failed to load the organization privacy policy:', error);
      const status = getErrorStatus(error);
      if (status === 403 || status === 404) unavailable = true;
      else loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function toggle(feature: Feature, next: boolean) {
    if (!policy || busy) return;
    busy = feature;
    try {
      policy = await OrgAdminApi.updatePolicy({ [FIELD[feature]]: next });
      toastStore.success($t('privacy.saved'));
    } catch (error) {
      console.error('Failed to save the organization privacy policy:', error);
      toastStore.error($t(privacyErrorKey(error, 'privacy.saveError')));
    } finally {
      busy = null;
    }
  }

  async function saveFloor(change: NonNullable<OrgPolicyChanges['redaction_floor']>) {
    if (!policy?.redaction_floor || busy) return;
    busy = 'floor';
    try {
      policy = await OrgAdminApi.updatePolicy({ redaction_floor: change });
      toastStore.success($t('privacy.saved'));
    } catch (error) {
      console.error('Failed to save the organization redaction floor:', error);
      toastStore.error($t(privacyErrorKey(error, 'privacy.saveError')));
    } finally {
      busy = null;
    }
  }

  function toggleCategory(category: FloorCategory, on: boolean) {
    const current = policy?.redaction_floor?.forced_categories ?? [];
    const next = on ? [...new Set([...current, category])] : current.filter((c) => c !== category);
    void saveFloor({ forced_categories: next });
  }

  function toggleEntity(entity: string, on: boolean) {
    const current = policy?.redaction_floor?.forced_pii_entities ?? [];
    const next = on ? [...new Set([...current, entity])] : current.filter((e) => e !== entity);
    void saveFloor({ forced_pii_entities: next });
  }

  function saveWords(text: string) {
    const words = text
      .split('\n')
      .map((w) => w.trim())
      .filter(Boolean);
    const current = policy?.redaction_floor?.forced_custom_words ?? [];
    if (words.join('\n') === current.join('\n')) return;
    void saveFloor({ forced_custom_words: words });
  }

  function saveThreshold(value: number) {
    if (value === policy?.redaction_floor?.force_toxicity_threshold) return;
    void saveFloor({ force_toxicity_threshold: value });
  }

  onMount(() => {
    void load();
  });
</script>

<div class="org-privacy" data-testid="org-privacy-panel">
  {#if loading}
    <SettingsSkeleton variant="rows" count={2} label={$t('common.loading')} />
  {:else if unavailable}
    <EmptyState
      icon="lock"
      title={$t('privacy.org.unavailableTitle')}
      description={$t('privacy.org.unavailableDescription')}
    />
  {:else if loadFailed || !policy}
    <p class="error" role="alert">{$t('privacy.org.loadError')}</p>
    <button type="button" class="btn btn-secondary" on:click={load}>{$t('settings.modal.sectionRetry')}</button>
  {:else}
    <p class="intro">{$t('privacy.org.intro')}</p>

    <PrivacySwitchCard
      id="org-voiceprints"
      title={$t('privacy.voiceprints.title')}
      description={$t('privacy.org.voiceprintsDescription')}
      checked={policy.voiceprint_matching_enabled}
      locked={lockedLayer('voiceprints') !== null}
      lockedReason={lockedReason('voiceprints')}
      busy={busy === 'voiceprints'}
      onToggle={(next) => toggle('voiceprints', next)}
      purgeLabel={$t('privacy.purge.voiceprintsButton')}
      purgeHint={policy.voiceprint_matching_enabled
        ? $t('privacy.purge.turnOffFirst')
        : $t('privacy.purge.orgVoiceprintsHint')}
      canPurge={!policy.voiceprint_matching_enabled}
      onPurge={() => (purging = ['voiceprints'])}
    />

    <PrivacySwitchCard
      id="org-gender"
      title={$t('privacy.gender.title')}
      description={$t('privacy.org.genderDescription')}
      notice={$t('privacy.gender.notice')}
      checked={policy.gender_inference_enabled}
      locked={lockedLayer('gender') !== null}
      lockedReason={lockedReason('gender')}
      busy={busy === 'gender'}
      onToggle={(next) => toggle('gender', next)}
      purgeLabel={$t('privacy.purge.genderButton')}
      purgeHint={policy.gender_inference_enabled
        ? $t('privacy.purge.turnOffFirst')
        : $t('privacy.purge.orgGenderHint')}
      canPurge={!policy.gender_inference_enabled}
      onPurge={() => (purging = ['inferred_gender'])}
    />

    {#if policy.redaction_floor}
      <section class="floor" data-testid="org-redaction-floor" aria-labelledby="org-floor-title">
        <h3 id="org-floor-title" class="floor-title">
          {$t('settings.redactionPolicy.forceCategoriesTitle')}
        </h3>
        <p class="intro">{$t('privacy.org.floorIntro')}</p>
        {#each FLOOR_CATEGORIES as cat (cat.id)}
          <label class="floor-row">
            <input
              type="checkbox"
              data-testid="org-floor-{cat.id}"
              checked={policy.redaction_floor.forced_categories.includes(cat.id)}
              disabled={busy !== null}
              on:change={(e) => toggleCategory(cat.id, e.currentTarget.checked)}
            />
            <span>{$t(cat.label)}</span>
          </label>
        {/each}
        {#if policy.redaction_floor.forced_categories.includes('toxicity')}
          <RangeSlider
            id="org-floor-threshold"
            label={$t('settings.redactionPolicy.forceToxicityThreshold')}
            min={0}
            max={1}
            step={0.05}
            hint={$t('settings.contentRedaction.toxicityThresholdHelp')}
            value={policy.redaction_floor.force_toxicity_threshold ?? DEFAULT_THRESHOLD}
            disabled={busy !== null}
            on:change={(e) => saveThreshold(Number((e.target as HTMLInputElement).value))}
          />
        {/if}

        <h4 class="floor-subtitle">{$t('settings.contentRedaction.piiEntities')}</h4>
        <div class="floor-entities">
          {#each PII_ENTITIES as entity (entity)}
            <label class="floor-row">
              <input
                type="checkbox"
                data-testid="org-floor-entity-{entity}"
                checked={policy.redaction_floor.forced_pii_entities.includes(entity)}
                disabled={busy !== null}
                on:change={(e) => toggleEntity(entity, e.currentTarget.checked)}
              />
              <span>{entity}</span>
            </label>
          {/each}
        </div>

        <label class="floor-words" for="org-floor-words">
          {$t('settings.contentRedaction.customWords')}
        </label>
        <textarea
          id="org-floor-words"
          class="form-textarea"
          data-testid="org-floor-words"
          rows="3"
          value={policy.redaction_floor.forced_custom_words.join('\n')}
          placeholder={$t('settings.contentRedaction.customWordsPlaceholder')}
          disabled={busy !== null}
          on:change={(e) => saveWords(e.currentTarget.value)}
        ></textarea>

        {#each FLOOR_BOOLEANS as flag (flag.id)}
          <label class="floor-row">
            <input
              type="checkbox"
              data-testid="org-floor-{flag.id}"
              checked={policy.redaction_floor[flag.id]}
              disabled={busy !== null}
              on:change={(e) => saveFloor({ [flag.id]: e.currentTarget.checked })}
            />
            <span>
              {$t(flag.label)}
              <small class="floor-help">{$t(flag.help)}</small>
            </span>
          </label>
        {/each}
      </section>
    {/if}
  {/if}

  <PurgeConfirmDialog
    isOpen={purging.length > 0}
    onClose={() => (purging = [])}
    targets={purging}
    scopeLabel={$t('privacy.purge.scopeOrganization')}
    start={() => OrgAdminApi.purgePolicyData(purging)}
  />
</div>

<style>
  .org-privacy {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-1) 0;
  }

  .intro {
    margin: 0;
    font-size: 0.8125rem;
    line-height: 1.5;
    color: var(--text-secondary);
    max-width: var(--measure-prose);
  }

  .floor {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--surface-color);
  }

  .floor-title {
    margin: 0;
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .floor-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .floor-subtitle {
    margin: var(--space-2) 0 0;
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .floor-entities {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(10rem, 1fr));
    gap: var(--space-1);
  }

  .floor-words {
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .floor-help {
    display: block;
    margin-top: var(--space-0-5);
    font-size: var(--font-size-xs);
    line-height: 1.4;
    color: var(--text-secondary);
  }

  .error {
    margin: 0;
    color: var(--error-color);
  }
</style>
