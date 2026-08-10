<!--
  FirstRunWizard.svelte — guided first-run auth setup (HANDOFF #28, auth-parity R8).

  Shown once to the bootstrap super_admin: a short flow that points at the three
  settings a first-time operator actually needs, out of 83 auth-related env vars
  and six admin tabs. It is a *presentation* of settings that already exist —
  password change lives in Settings → Profile, SSO/LDAP setup lives in
  Settings → Authentication, both reused via SettingsModal rather than
  reimplemented here. Only the security-default checkboxes (step 3) write
  config directly, since those have no dedicated small form of their own.

  Never blocks anything: if the status fetch fails, or a super_admin dismisses
  it, the rest of the app is unaffected — see first_run_wizard.py's docstring.
-->
<script>
  import { onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { user } from '$stores/auth';
  import { settingsModalStore } from '../stores/settingsModalStore';
  import { firstRunWizardStore } from '../stores/firstRunWizardStore';
  import { toastStore } from '../stores/toast';
  import { t } from '$stores/locale';
  import BaseModal from './ui/BaseModal.svelte';

  let visible = false;
  let step = 1;
  let saving = false;

  /** @type {'solo'|'sso'|'directory'|null} */
  let posture = null;

  let mfaRequired = false;
  let loginBannerEnabled = false;
  let requireApproval = false;

  // Settings → Authentication's "re-run setup wizard" entry point (HANDOFF
  // #28: "re-runnable... for someone who skipped it"). Ignores the initial
  // value on mount so this doesn't fire before the store has ever been bumped.
  let sawInitialReopenToken = false;
  $: if ($firstRunWizardStore) {
    if (!sawInitialReopenToken) {
      sawInitialReopenToken = true;
    } else {
      step = 1;
      posture = null;
      visible = true;
    }
  }

  onMount(async () => {
    if (!$user || $user.role !== 'super_admin') return;
    try {
      const { data } = await axiosInstance.get('/admin/first-run-wizard/status');
      visible = !data.completed_at;
    } catch (err) {
      // Never block the app over this — see the endpoint's own docstring.
      console.error('Error checking first-run wizard status:', err);
    }
  });

  async function markComplete() {
    try {
      await axiosInstance.post('/admin/first-run-wizard/complete');
    } catch (err) {
      console.error('Error marking first-run wizard complete:', err);
    } finally {
      visible = false;
    }
  }

  function skip() {
    markComplete();
  }

  // Step onto the settings page the wizard is pointing at, rather than leaving
  // this modal open on top of it -- it was blocking the very form the button
  // exists to reach. Not marked complete: the status check on next mount
  // re-shows the wizard, and "Re-run setup wizard" in Settings -> Authentication
  // is always available in the meantime.
  function openProfileSettings() {
    visible = false;
    settingsModalStore.open('profile');
  }

  function openAuthenticationSettings() {
    visible = false;
    settingsModalStore.open('authentication');
  }

  /** @param {'solo'|'sso'|'directory'} choice */
  async function choosePosture(choice) {
    posture = choice;
    if (choice === 'solo') {
      try {
        await axiosInstance.put('/admin/auth-config/local', { allow_registration: false });
      } catch (err) {
        console.error('Error applying solo/small-team posture:', err);
        toastStore.error($t('firstRunWizard.postureApplyFailed'));
      }
    }
    step = 3;
  }

  async function applySecurityDefaultsAndFinish() {
    saving = true;
    try {
      if (mfaRequired) {
        await axiosInstance.put('/admin/auth-config/mfa', {
          mfa_enabled: true,
          mfa_required: true
        });
      }
      if (loginBannerEnabled) {
        await axiosInstance.put('/admin/auth-config/banner', { login_banner_enabled: true });
      }
      if (requireApproval) {
        await axiosInstance.put('/admin/auth-config/local', { require_account_approval: true });
      }
      await markComplete();
      toastStore.success($t('firstRunWizard.setupComplete'));
    } catch (err) {
      console.error('Error applying first-run security defaults:', err);
      toastStore.error($t('firstRunWizard.securityDefaultsFailed'));
    } finally {
      saving = false;
    }
  }
</script>

<BaseModal isOpen={visible} title={$t('firstRunWizard.title')} maxWidth="640px" onClose={skip}>
  {#if step === 1}
    <div class="wizard-step">
      <p>{$t('firstRunWizard.welcomeBody')}</p>
      <button class="btn btn-secondary" on:click={openProfileSettings}>
        {$t('firstRunWizard.changePasswordButton')}
      </button>
    </div>
  {:else if step === 2}
    <div class="wizard-step">
      <p>{$t('firstRunWizard.postureIntro')}</p>
      <div class="posture-cards">
        <button
          class="posture-card"
          class:selected={posture === 'solo'}
          on:click={() => choosePosture('solo')}
        >
          <strong>{$t('firstRunWizard.postureSoloTitle')}</strong>
          <span>{$t('firstRunWizard.postureSoloBody')}</span>
        </button>
        <button
          class="posture-card"
          class:selected={posture === 'sso'}
          on:click={() => choosePosture('sso')}
        >
          <strong>{$t('firstRunWizard.postureSsoTitle')}</strong>
          <span>{$t('firstRunWizard.postureSsoBody')}</span>
        </button>
        <button
          class="posture-card"
          class:selected={posture === 'directory'}
          on:click={() => choosePosture('directory')}
        >
          <strong>{$t('firstRunWizard.postureDirectoryTitle')}</strong>
          <span>{$t('firstRunWizard.postureDirectoryBody')}</span>
        </button>
      </div>
      {#if posture === 'sso' || posture === 'directory'}
        <button class="btn btn-secondary" on:click={openAuthenticationSettings}>
          {$t('firstRunWizard.openAuthSettingsButton')}
        </button>
      {/if}
    </div>
  {:else if step === 3}
    <div class="wizard-step">
      <p>{$t('firstRunWizard.securityDefaultsIntro')}</p>
      <label class="wizard-checkbox">
        <input type="checkbox" bind:checked={mfaRequired} />
        <span>
          <strong>{$t('firstRunWizard.mfaRequiredLabel')}</strong>
          <small>{$t('firstRunWizard.mfaRequiredBody')}</small>
        </span>
      </label>
      <label class="wizard-checkbox">
        <input type="checkbox" bind:checked={loginBannerEnabled} />
        <span>
          <strong>{$t('firstRunWizard.loginBannerLabel')}</strong>
          <small>{$t('firstRunWizard.loginBannerBody')}</small>
        </span>
      </label>
      <label class="wizard-checkbox">
        <input type="checkbox" bind:checked={requireApproval} />
        <span>
          <strong>{$t('firstRunWizard.requireApprovalLabel')}</strong>
          <small>{$t('firstRunWizard.requireApprovalBody')}</small>
        </span>
      </label>
    </div>
  {/if}

  <svelte:fragment slot="footer">
    <button class="wizard-skip-link" on:click={skip} disabled={saving}>
      {$t('firstRunWizard.skipButton')}
    </button>
    <div class="wizard-footer-spacer"></div>
    {#if step > 1}
      <button class="btn btn-secondary" on:click={() => (step -= 1)} disabled={saving}>
        {$t('common.back')}
      </button>
    {/if}
    {#if step < 3}
      <button class="btn btn-primary" on:click={() => (step += 1)} disabled={saving}>
        {$t('common.next')}
      </button>
    {:else}
      <button class="btn btn-primary" on:click={applySecurityDefaultsAndFinish} disabled={saving}>
        {saving ? $t('firstRunWizard.applying') : $t('firstRunWizard.finishButton')}
      </button>
    {/if}
  </svelte:fragment>
</BaseModal>

<style>
  .wizard-step {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .posture-cards {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .posture-card {
    display: flex;
    flex-direction: column;
    gap: 4px;
    text-align: left;
    padding: 14px 16px;
    border: 2px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background-color: var(--bg-secondary, white);
    color: var(--text-primary, #111827);
    cursor: pointer;
  }

  .posture-card.selected {
    border-color: #3b82f6;
  }

  .posture-card span {
    font-size: 0.85rem;
    color: var(--text-secondary, #6b7280);
  }

  .wizard-checkbox {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    cursor: pointer;
  }

  .wizard-checkbox input {
    margin-top: 3px;
  }

  .wizard-checkbox small {
    display: block;
    color: var(--text-secondary, #6b7280);
    font-weight: normal;
  }

  .wizard-footer-spacer {
    flex: 1;
  }

  .wizard-skip-link {
    background: none;
    border: none;
    color: var(--text-secondary, #6b7280);
    text-decoration: underline;
    cursor: pointer;
    padding: 8px 4px;
    font-size: 0.9rem;
  }

  .wizard-skip-link:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
