<!--
  InstancePrivacyCard.svelte — the deployment-wide privacy switches (issue #1280, super_admin).

  The instance layer of the restrict-only policy: voiceprint matching and gender inference for
  everyone on this deployment. A switch off here cannot be turned back on by an organization or a
  user. Switching off hides data; the purge deletes it, and only offers once the switch is off.

  Rendered on the Privacy & Redaction "policy" tab, which is already super_admin only, so this
  makes no request for anyone else. In a multi-tenant deployment the server refuses a purge of
  "everything" (a platform administrator must not destroy tenant data), so the choice is not
  offered there; the refusal is still shown if it arrives.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import PrivacySwitchCard from '$components/privacy/PrivacySwitchCard.svelte';
  import PurgeConfirmDialog from '$components/privacy/PurgeConfirmDialog.svelte';
  import {
    PrivacyApi,
    type InstancePrivacyPolicy,
    type PurgeScope,
    type PurgeTarget,
  } from '$lib/api/privacy';
  import { privacyErrorKey } from '$lib/settings/privacyPolicy';
  import { capabilities } from '$stores/capabilities';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  type Feature = 'voiceprints' | 'gender';

  let policy: InstancePrivacyPolicy | null = null;
  let loading = true;
  let loadFailed = false;
  let busy: Feature | null = null;
  let purging: PurgeTarget[] = [];
  let scope: PurgeScope = 'personal';

  $: multiTenant = $capabilities.tenancyMode === 'multi';
  // Fail closed: "everything" is offered only when the deployment is known to be single-tenant.
  $: canPurgeEverything = $capabilities.tenancyMode === 'single';
  $: if (!canPurgeEverything && scope === 'all') scope = 'personal';

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      policy = await PrivacyApi.getInstance();
    } catch (error) {
      console.error('Failed to load the instance privacy policy:', error);
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function toggle(feature: Feature, next: boolean) {
    if (!policy || busy) return;
    busy = feature;
    try {
      const changes =
        feature === 'voiceprints'
          ? { voiceprint_matching_enabled: next }
          : { gender_inference_enabled: next };
      policy = await PrivacyApi.updateInstance(changes);
      toastStore.success($t('privacy.saved'));
    } catch (error) {
      console.error('Failed to save the instance privacy policy:', error);
      toastStore.error($t(privacyErrorKey(error, 'privacy.saveError')));
    } finally {
      busy = null;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<div class="instance-privacy" data-testid="instance-privacy-card">
  <h4 class="heading">{$t('privacy.instance.title')}</h4>
  <p class="intro">{$t('privacy.instance.intro')}</p>

  {#if loading}
    <SettingsSkeleton variant="rows" count={2} label={$t('common.loading')} />
  {:else if loadFailed || !policy}
    <p class="error" role="alert">{$t('privacy.instance.loadError')}</p>
    <button type="button" class="btn btn-secondary" on:click={load}>
      {$t('settings.modal.sectionRetry')}
    </button>
  {:else}
    <PrivacySwitchCard
      id="instance-voiceprints"
      title={$t('privacy.voiceprints.title')}
      description={$t('privacy.instance.voiceprintsDescription')}
      checked={policy.voiceprint_matching_enabled}
      busy={busy === 'voiceprints'}
      onToggle={(next) => toggle('voiceprints', next)}
      purgeLabel={$t('privacy.purge.voiceprintsButton')}
      purgeHint={policy.voiceprint_matching_enabled
        ? $t('privacy.purge.turnOffFirst')
        : $t('privacy.purge.instanceHint')}
      canPurge={!policy.voiceprint_matching_enabled}
      onPurge={() => (purging = ['voiceprints'])}
    />

    <PrivacySwitchCard
      id="instance-gender"
      title={$t('privacy.gender.title')}
      description={$t('privacy.instance.genderDescription')}
      notice={$t('privacy.gender.notice')}
      checked={policy.gender_inference_enabled}
      busy={busy === 'gender'}
      onToggle={(next) => toggle('gender', next)}
      purgeLabel={$t('privacy.purge.genderButton')}
      purgeHint={policy.gender_inference_enabled
        ? $t('privacy.purge.turnOffFirst')
        : $t('privacy.purge.instanceHint')}
      canPurge={!policy.gender_inference_enabled}
      onPurge={() => (purging = ['inferred_gender'])}
    />

    <fieldset class="scope">
      <legend>{$t('privacy.instance.scopeLegend')}</legend>
      <label>
        <input type="radio" name="instance-purge-scope" value="personal" bind:group={scope} />
        <span>{$t('privacy.instance.scopePersonal')}</span>
      </label>
      {#if canPurgeEverything}
        <label>
          <input type="radio" name="instance-purge-scope" value="all" bind:group={scope} />
          <span>{$t('privacy.instance.scopeAll')}</span>
        </label>
      {:else if multiTenant}
        <small data-testid="instance-scope-multi">{$t('privacy.instance.scopeMultiTenant')}</small>
      {/if}
    </fieldset>
  {/if}

  <PurgeConfirmDialog
    isOpen={purging.length > 0}
    onClose={() => (purging = [])}
    targets={purging}
    scopeLabel={scope === 'all'
      ? $t('privacy.purge.scopeInstanceAll')
      : $t('privacy.purge.scopeInstancePersonal')}
    start={() => PrivacyApi.purgeInstance(scope, purging)}
  />
</div>

<style>
  .instance-privacy {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.5rem 0 1.5rem;
  }

  .heading {
    margin: 0;
    font-size: 1rem;
    font-weight: var(--font-weight-semibold, 600);
    color: var(--text-color);
  }

  .intro {
    margin: 0;
    font-size: 0.8125rem;
    line-height: 1.5;
    color: var(--text-secondary);
    max-width: var(--measure-prose, 72ch);
  }

  .scope {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    font-size: 0.8125rem;
    color: var(--text-color);
  }

  .scope legend {
    padding: 0 0.375rem;
    font-weight: var(--font-weight-medium, 500);
  }

  .scope label {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    cursor: pointer;
  }

  small {
    color: var(--text-secondary);
  }

  .error {
    margin: 0;
    color: var(--error-color, #dc2626);
  }
</style>
