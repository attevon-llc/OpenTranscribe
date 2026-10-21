<!--
  FirstRunWizard.svelte — guided first-run auth setup (HANDOFF #28, auth-parity R8).

  Shown once to the bootstrap super_admin: a short flow that points at the three
  settings a first-time operator actually needs, out of 83 auth-related env vars
  and six admin tabs. It is a *presentation* of settings that already exist —
  password change lives in Settings → Profile, SSO/LDAP setup lives in
  Settings → Authentication, both reused via SettingsModal rather than
  reimplemented here. Only the security-default checkboxes (step 3) write
  config directly, since those have no dedicated small form of their own.

  Four steps -- password, deployment posture, security defaults, summary -- with
  a visible indicator and a Back button. Completion is recorded only on the last
  step's Finish or an explicit Skip; closing the dialog just hides it. The
  password step hands off to Settings -> Profile and resumes when that closes.

  Never blocks anything: if the status fetch fails, or a super_admin dismisses
  it, the rest of the app is unaffected — see first_run_wizard.py's docstring.
-->
<script>
  import { onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { user } from '$stores/auth';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { firstRunWizardStore } from '$stores/firstRunWizardStore';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import BaseModal from './ui/BaseModal.svelte';

  const TOTAL_STEPS = 4;

  let visible = false;
  let step = 1;
  let saving = false;

  /** @type {'default_dev'|'operator_set'|'generated'|null} */
  let credentialKind = null;
  let minAgeHours = 0;
  let passwordChanged = false;
  let passwordKept = false;
  // Set when we come back from Settings and the password is still the old one.
  let returnedUnchanged = false;

  /** @type {'solo'|'sso'|'directory'|null} */
  let posture = null;

  let mfaRequired = false;
  let loginBannerEnabled = false;
  let requireApproval = false;
  let defaultsApplied = false;
  /** @type {string[]} */
  let appliedDefaults = [];

  $: passwordDone = passwordChanged || passwordKept;
  $: steps = [
    { id: 'password', label: $t('firstRunWizard.stepPassword'), done: passwordDone },
    { id: 'posture', label: $t('firstRunWizard.stepPosture'), done: posture !== null },
    { id: 'defaults', label: $t('firstRunWizard.stepDefaults'), done: defaultsApplied },
    { id: 'summary', label: $t('firstRunWizard.stepSummary'), done: false }
  ];
  $: postureTitle =
    posture === 'solo'
      ? $t('firstRunWizard.postureSoloTitle')
      : posture === 'sso'
        ? $t('firstRunWizard.postureSsoTitle')
        : posture === 'directory'
          ? $t('firstRunWizard.postureDirectoryTitle')
          : '';

  // Settings -> Authentication's "re-run setup wizard" entry point (HANDOFF
  // #28: "re-runnable... for someone who skipped it"). Ignores the initial
  // value on mount so this doesn't fire before the store has ever been bumped.
  let sawInitialReopenToken = false;
  $: if ($firstRunWizardStore) {
    if (!sawInitialReopenToken) {
      sawInitialReopenToken = true;
    } else {
      step = 1;
      posture = null;
      passwordKept = false;
      returnedUnchanged = false;
      defaultsApplied = false;
      appliedDefaults = [];
      refreshStatus();
      visible = true;
    }
  }

  async function refreshStatus() {
    try {
      const { data } = await axiosInstance.get('/admin/first-run-wizard/status');
      credentialKind = data.credential_kind ?? null;
      minAgeHours = data.password_min_age_hours ?? 0;
      passwordChanged = Boolean(data.password_changed_by_you);
      return data;
    } catch (err) {
      // Never block the app over this -- see the endpoint's own docstring.
      console.error('Error checking first-run wizard status:', err);
      return null;
    }
  }

  onMount(async () => {
    if (!$user || $user.role !== 'super_admin') return;
    const data = await refreshStatus();
    if (data) visible = !data.completed_at;
  });

  // The wizard hides itself while Settings -> Profile is open (it would sit on
  // top of the form), then resumes here once Settings closes.
  let awaitingProfile = false;
  let profileWasOpen = false;
  $: onSettingsOpenChange($settingsModalStore.isOpen);

  /** @param {boolean} isOpen */
  function onSettingsOpenChange(isOpen) {
    if (!awaitingProfile) return;
    if (isOpen) {
      profileWasOpen = true;
      return;
    }
    if (!profileWasOpen) return;
    awaitingProfile = false;
    profileWasOpen = false;
    resumeAfterProfile();
  }

  async function resumeAfterProfile() {
    const wasChanged = passwordChanged;
    await refreshStatus();
    returnedUnchanged = !passwordChanged;
    if (passwordChanged && !wasChanged) passwordKept = false;
    step = passwordChanged ? 2 : 1;
    visible = true;
  }

  async function markComplete() {
    try {
      await axiosInstance.post('/admin/first-run-wizard/complete');
    } catch (err) {
      console.error('Error marking first-run wizard complete:', err);
    } finally {
      visible = false;
    }
  }

  // Explicit Skip: records completion without applying anything further.
  function skip() {
    markComplete();
  }

  // The dialog's own close control only hides it; completion is never implied.
  function hide() {
    visible = false;
  }

  function openProfileSettings() {
    awaitingProfile = true;
    profileWasOpen = false;
    visible = false;
    settingsModalStore.open('profile');
  }

  function keepCurrentPassword() {
    passwordKept = true;
    step = 2;
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

  async function applySecurityDefaults() {
    saving = true;
    try {
      /** @type {string[]} */
      const applied = [];
      if (mfaRequired) {
        await axiosInstance.put('/admin/auth-config/mfa', {
          mfa_enabled: true,
          mfa_required: true
        });
        applied.push($t('firstRunWizard.mfaRequiredLabel'));
      }
      if (loginBannerEnabled) {
        await axiosInstance.put('/admin/auth-config/banner', { login_banner_enabled: true });
        applied.push($t('firstRunWizard.loginBannerLabel'));
      }
      if (requireApproval) {
        await axiosInstance.put('/admin/auth-config/local', { require_account_approval: true });
        applied.push($t('firstRunWizard.requireApprovalLabel'));
      }
      appliedDefaults = applied;
      defaultsApplied = true;
      step = 4;
    } catch (err) {
      console.error('Error applying first-run security defaults:', err);
      toastStore.error($t('firstRunWizard.securityDefaultsFailed'));
    } finally {
      saving = false;
    }
  }

  async function finish() {
    saving = true;
    await markComplete();
    saving = false;
    toastStore.success($t('firstRunWizard.setupComplete'));
  }
</script>

<BaseModal isOpen={visible} title={$t('firstRunWizard.title')} size="lg" onClose={hide}>
  <div class="wizard-progress" data-testid="wizard-step-indicator" data-step={step}>
    <p class="wizard-progress-label">
      {$t('firstRunWizard.stepIndicator', { current: step, total: TOTAL_STEPS })}
    </p>
    <ol class="wizard-progress-list">
      {#each steps as item, index (item.id)}
        <li class:current={step === index + 1} class:done={item.done} data-done={item.done}>
          <span class="wizard-progress-mark" aria-hidden="true">{item.done ? '✓' : index + 1}</span>
          <span>{item.label}</span>
          {#if item.done}
            <span class="sr-only">{$t('firstRunWizard.stepDoneMark')}</span>
          {/if}
        </li>
      {/each}
    </ol>
  </div>

  {#if step === 1}
    <div class="wizard-step">
      <p>{$t('firstRunWizard.welcomeBody')}</p>
      <p>{$t('firstRunWizard.whyBody')}</p>

      {#if credentialKind === 'default_dev'}
        <div class="wizard-callout wizard-callout-warning" role="alert" data-testid="cred-default-dev">
          <strong>{$t('firstRunWizard.defaultWarningTitle')}</strong>
          <span>{$t('firstRunWizard.defaultWarningBody')}</span>
        </div>
      {:else if credentialKind === 'generated'}
        <div class="wizard-callout" data-testid="cred-generated">
          <span>{$t('firstRunWizard.generatedNote')}</span>
        </div>
      {:else if credentialKind === 'operator_set'}
        <div class="wizard-callout" data-testid="cred-operator-set">
          <span>{$t('firstRunWizard.operatorSetNote')}</span>
        </div>
      {/if}

      {#if passwordChanged}
        <div class="wizard-callout wizard-callout-success" data-testid="password-changed">
          <span>{$t('firstRunWizard.passwordChangedNote')}</span>
        </div>
      {:else}
        {#if returnedUnchanged}
          <div class="wizard-callout" role="status" data-testid="password-unchanged">
            <span>{$t('firstRunWizard.passwordUnchangedNote')}</span>
          </div>
        {/if}
        <p class="wizard-hint">{$t('firstRunWizard.changeButtonExplain')}</p>
        {#if minAgeHours > 0}
          <p class="wizard-hint">{$t('firstRunWizard.minAgeNote', { hours: minAgeHours })}</p>
        {:else}
          <p class="wizard-hint">{$t('firstRunWizard.minAgeNoteGeneric')}</p>
        {/if}
        <div class="wizard-actions">
          <button class="btn btn-primary" on:click={openProfileSettings}>
            {$t('firstRunWizard.changePasswordButton')}
          </button>
          <button class="btn btn-secondary" on:click={keepCurrentPassword}>
            {$t('firstRunWizard.keepPasswordButton')}
          </button>
        </div>
      {/if}
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
  {:else}
    <div class="wizard-step" data-testid="wizard-summary">
      <p>{$t('firstRunWizard.summaryIntro')}</p>
      <ul class="wizard-summary">
        <li>
          <strong>{$t('firstRunWizard.stepPassword')}:</strong>
          {#if passwordChanged}
            {$t('firstRunWizard.summaryPasswordChanged')}
          {:else if passwordKept}
            {$t('firstRunWizard.summaryPasswordKept')}
          {:else}
            {$t('firstRunWizard.summaryNotSet')}
          {/if}
        </li>
        <li>
          <strong>{$t('firstRunWizard.stepPosture')}:</strong>
          {#if posture}
            {postureTitle}
          {:else}
            {$t('firstRunWizard.summaryNotSet')}
          {/if}
        </li>
        <li>
          <strong>{$t('firstRunWizard.stepDefaults')}:</strong>
          {#if appliedDefaults.length}
            {appliedDefaults.join(', ')}
          {:else}
            {$t('firstRunWizard.summaryNoDefaults')}
          {/if}
        </li>
      </ul>
      <p>{$t('firstRunWizard.summaryNextSteps')}</p>
    </div>
  {/if}

  {#if step < 4}
    <p class="wizard-hint wizard-skip-hint">{$t('firstRunWizard.skipHint')}</p>
  {/if}

  <svelte:fragment slot="footer">
    {#if step < 4}
      <button class="wizard-skip-link" on:click={skip} disabled={saving}>
        {$t('firstRunWizard.skipButton')}
      </button>
    {/if}
    <div class="wizard-footer-spacer"></div>
    {#if step > 1}
      <button class="btn btn-secondary" on:click={() => (step -= 1)} disabled={saving}>
        {$t('common.back')}
      </button>
    {/if}
    {#if step === 1}
      {#if passwordChanged}
        <button class="btn btn-primary" on:click={() => (step = 2)}>{$t('common.next')}</button>
      {/if}
    {:else if step === 2}
      <button class="btn btn-primary" on:click={() => (step = 3)} disabled={saving}>
        {$t('common.next')}
      </button>
    {:else if step === 3}
      <button class="btn btn-primary" on:click={applySecurityDefaults} disabled={saving}>
        {saving ? $t('firstRunWizard.applying') : $t('firstRunWizard.applyButton')}
      </button>
    {:else}
      <button class="btn btn-primary" on:click={finish} disabled={saving}>
        {$t('firstRunWizard.finishButton')}
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

  .wizard-progress {
    margin-block-end: 16px;
  }

  .wizard-progress-label {
    margin: 0 0 8px;
    color: var(--text-secondary, #6b7280);
    font-size: var(--font-size-sm, 0.85rem);
  }

  .wizard-progress-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: var(--font-size-sm, 0.85rem);
    color: var(--text-secondary, #6b7280);
  }

  .wizard-progress-list li {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .wizard-progress-list li.current {
    color: var(--text-primary, #111827);
    font-weight: var(--font-weight-semibold, 600);
  }

  .wizard-progress-mark {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 50%;
    font-size: var(--font-size-xs, 0.75rem);
  }

  .wizard-progress-list li.done .wizard-progress-mark {
    border-color: var(--success-color, #10b981);
    background-color: rgba(var(--success-color-rgb, 16, 185, 129), 0.15);
    color: var(--text-primary, #111827);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }

  .wizard-callout {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 12px 14px;
    border: 1px solid var(--border-color, #d1d5db);
    border-inline-start-width: 4px;
    border-radius: 8px;
    background-color: var(--bg-secondary, white);
    color: var(--text-primary, #111827);
  }

  .wizard-callout-warning {
    border-color: var(--warning-color, #f59e0b);
    background-color: rgba(var(--warning-color-rgb, 245, 158, 11), 0.12);
  }

  .wizard-callout-success {
    border-color: var(--success-color, #10b981);
    background-color: rgba(var(--success-color-rgb, 16, 185, 129), 0.12);
  }

  .wizard-hint {
    margin: 0;
    color: var(--text-secondary, #6b7280);
    font-size: var(--font-size-sm, 0.9rem);
  }

  .wizard-skip-hint {
    margin-block-start: 20px;
  }

  .wizard-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }

  .wizard-summary {
    margin: 0;
    padding-inline-start: 20px;
    display: flex;
    flex-direction: column;
    gap: 6px;
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
    text-align: start;
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
