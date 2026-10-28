<!--
  UserPrivacyCard.svelte — a person's own voice privacy (issue #1280), under Voice Attributes.

  Two things live here. The voiceprint switch ("recognize speakers across my recordings") is
  this card's own: it is the user layer of the restrict-only policy, so when the organization,
  deployment or platform has it off the switch is disabled and says who. The gender switch stays
  with the other voice attributes above (one switch, one place); this card only offers the
  deletion of what was inferred once that switch is off, and needs `reload()` after it changes.

  Switching off hides; deleting is the separate purge, confirmed by typing a word.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import PrivacySwitchCard from '$components/privacy/PrivacySwitchCard.svelte';
  import PurgeConfirmDialog from '$components/privacy/PurgeConfirmDialog.svelte';
  import { PrivacyApi, type PurgeTarget, type UserPrivacyPolicy } from '$lib/api/privacy';
  import { getSpeakerAttributeSettings } from '$lib/api/speakerAttributeSettings';
  import { LOCKED_BY_KEYS, privacyErrorKey } from '$lib/settings/privacyPolicy';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  let mine: UserPrivacyPolicy | null = null;
  /** The person's own gender switch; unknown (null) until read, which keeps the purge disabled. */
  let genderEnabled: boolean | null = null;
  let loading = true;
  let loadFailed = false;
  let busy = false;
  let purging: PurgeTarget[] = [];

  async function readGender() {
    try {
      genderEnabled = (await getSpeakerAttributeSettings()).gender_detection_enabled;
    } catch (error) {
      console.error('Failed to read the gender setting:', error);
      genderEnabled = null;
    }
  }

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      const [policy] = await Promise.all([PrivacyApi.getMine(), readGender()]);
      mine = policy;
    } catch (error) {
      console.error('Failed to load the voice privacy setting:', error);
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  /** Called by the parent after the gender switch is saved or reset. */
  export async function reload() {
    await readGender();
  }

  async function toggle(next: boolean) {
    if (!mine || busy) return;
    busy = true;
    try {
      mine = await PrivacyApi.setMine(next);
      toastStore.success($t('privacy.saved'));
    } catch (error) {
      console.error('Failed to save the voice privacy setting:', error);
      toastStore.error($t(privacyErrorKey(error, 'privacy.saveError')));
      void load();
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<div class="user-privacy" data-testid="user-privacy-card">
  {#if loading}
    <SettingsSkeleton variant="rows" count={2} label={$t('common.loading')} />
  {:else if loadFailed || !mine}
    <p class="error" role="alert">{$t('privacy.user.loadError')}</p>
  {:else}
    <PrivacySwitchCard
      id="user-voiceprints"
      title={$t('privacy.user.voiceprintsTitle')}
      description={$t('privacy.user.voiceprintsDescription')}
      checked={mine.voiceprint_matching_enabled}
      locked={mine.locked_by !== null}
      lockedReason={mine.locked_by ? $t(LOCKED_BY_KEYS[mine.locked_by]) : ''}
      {busy}
      onToggle={toggle}
      purgeLabel={$t('privacy.purge.mineVoiceprintsButton')}
      purgeHint={mine.voiceprint_matching_enabled
        ? $t('privacy.purge.turnOffFirst')
        : $t('privacy.purge.mineHint')}
      canPurge={!mine.voiceprint_matching_enabled}
      onPurge={() => (purging = ['voiceprints'])}
    />

    <PrivacySwitchCard
      id="user-gender"
      showSwitch={false}
      title={$t('privacy.gender.title')}
      description={$t('privacy.user.genderPurgeDescription')}
      checked={genderEnabled ?? true}
      onToggle={() => {}}
      purgeLabel={$t('privacy.purge.mineGenderButton')}
      purgeHint={genderEnabled === false
        ? $t('privacy.purge.mineHint')
        : $t('privacy.purge.turnOffGenderAbove')}
      canPurge={genderEnabled === false}
      onPurge={() => (purging = ['inferred_gender'])}
    />
  {/if}

  <PurgeConfirmDialog
    isOpen={purging.length > 0}
    onClose={() => (purging = [])}
    targets={purging}
    scopeLabel={$t('privacy.purge.scopeMine')}
    start={() => PrivacyApi.purgeMine(purging)}
  />
</div>

<style>
  .user-privacy {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    margin-top: 1.5rem;
  }

  .error {
    margin: 0;
    color: var(--error-color, #dc2626);
  }
</style>
