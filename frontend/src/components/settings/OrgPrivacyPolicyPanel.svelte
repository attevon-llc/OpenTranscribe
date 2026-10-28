<!--
  OrgPrivacyPolicyPanel.svelte — an organization administrator's privacy policy (issue #1280).

  Two restrict-only switches for everyone in the organization: matching speakers by voice, and
  inferring gender from voice. A layer above the organization (the deployment or the platform)
  can already have a feature off; this panel then shows the switch disabled and says who turned
  it off. Switching a feature off hides data; the separate purge deletes it and is only offered
  once the switch is off. The server decides every outcome, this renders it.

  The organization comes from the session on the server; there is no organization parameter.
  The redaction floor (#1279) is a later release and has no control here.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import PrivacySwitchCard from '$components/privacy/PrivacySwitchCard.svelte';
  import PurgeConfirmDialog from '$components/privacy/PurgeConfirmDialog.svelte';
  import { OrgAdminApi, type OrgPolicy, type OrgPolicyChanges } from '$lib/api/orgAdmin';
  import type { PrivacyDecision, PurgeTarget } from '$lib/api/privacy';
  import { getErrorStatus } from '$lib/utils/apiError';
  import {
    LOCKED_BY_KEYS,
    isLockedAboveOrganization,
    privacyErrorKey,
  } from '$lib/settings/privacyPolicy';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  type Feature = 'voiceprints' | 'gender';

  let policy: OrgPolicy | null = null;
  let loading = true;
  let unavailable = false;
  let loadFailed = false;
  let busy: Feature | null = null;
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
    gap: 1rem;
    padding: 0.25rem 0;
  }

  .intro {
    margin: 0;
    font-size: 0.8125rem;
    line-height: 1.5;
    color: var(--text-secondary);
    max-width: var(--measure-prose, 72ch);
  }

  .error {
    margin: 0;
    color: var(--error-color, #dc2626);
  }
</style>
