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
  a visible indicator and a Back button. When the deployment ships legal documents
  (status.legal_required) and the operator has not yet accepted them, a "Terms and
  responsibilities" step comes first (five steps); it cannot be skipped. Completion is recorded only on the last
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
  import MenuIcon from './ui/MenuIcon.svelte';
  import Stepper from './ui/Stepper.svelte';
  import LegalDocumentViewer from '$components/legal/LegalDocumentViewer.svelte';
  import LegalProse from '$components/legal/LegalProse.svelte';
  import {
    OPERATOR_ACK_KEY,
    TERMS_KEY,
    PRIVACY_KEY,
    getLegalDocument,
    acknowledgeLegalDocument
  } from '$lib/api/legal';
  import { AuthConfigApi } from '$lib/api/authConfig';
  import { renderLegalMarkdown } from '$lib/legal/renderLegalMarkdown';
  import { extractCheckboxText } from '$lib/legal/checkboxText';

  let visible = false;
  // 0 is the terms step (only when includeTerms); 1..4 are the original steps.
  let step = 1;
  let saving = false;

  // Sticky once set, so accepting the terms does not renumber the steps under the user.
  let includeTerms = false;
  let termsAccepted = false;
  /** @type {{key:string,title:string,version:string,status:string,sha256:string,markdown:string}|null} */
  let termsDoc = null;
  let termsLoadFailed = false;
  let termsChecked = false;
  let viewerKey = '';
  let viewerOpen = false;
  let viewerFullscreen = false;

  /**
   * Whether auth mail can be delivered: true / false, or null when it could not be read
   * (the summary line is then omitted rather than guessed). Read each time the summary
   * is entered, not on mount: the operator may leave for Settings -> Auth Email, fix it
   * and come back, and a value read before they left would still say it is broken.
   * @type {boolean|null}
   */
  let mailDeliverable = null;
  $: if (step === 4) void refreshMailReadiness();

  async function refreshMailReadiness() {
    const readiness = await AuthConfigApi.getAuthMailReadiness();
    mailDeliverable = typeof readiness?.deliverable === 'boolean' ? readiness.deliverable : null;
  }

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

  // The dialog body keeps its scroll position across steps; without this the step after the
  // long terms text opened scrolled to its middle, with the indicator out of view.
  /** @type {HTMLElement|null} */
  let progressEl = null;
  let shownStep = step;
  $: onStepShown(step);

  /** @param {number} next */
  function onStepShown(next) {
    if (next === shownStep) return;
    shownStep = next;
    progressEl?.scrollIntoView?.({ block: 'nearest' });
  }

  $: passwordDone = passwordChanged || passwordKept;
  $: offset = includeTerms ? 1 : 0;
  $: totalSteps = 4 + offset;
  let termsHtml = '';
  $: void renderTermsHtml(termsDoc);

  async function renderTermsHtml(current) {
    const rendered = current ? await renderLegalMarkdown(current.markdown) : '';
    if (current === termsDoc) termsHtml = rendered;
  }
  $: checkboxText = extractCheckboxText(termsDoc?.markdown) ?? $t('firstRunWizard.termsAcceptFallback');
  $: steps = [
    ...(includeTerms
      ? [{ id: 'terms', label: $t('firstRunWizard.stepTerms'), done: termsAccepted }]
      : []),
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
  // Only the token is a reactive dependency; a reactive block that read wizard state
  // directly re-ran (and reopened the wizard) every time that state changed.
  $: onReopenRequest($firstRunWizardStore);

  /** @param {unknown} token */
  function onReopenRequest(token) {
    if (!token) return;
    if (!sawInitialReopenToken) {
      sawInitialReopenToken = true;
      return;
    }
    posture = null;
    passwordKept = false;
    returnedUnchanged = false;
    defaultsApplied = false;
    appliedDefaults = [];
    refreshStatus().then(() => {
      step = includeTerms && !termsAccepted ? 0 : 1;
    });
    visible = true;
  }

  async function refreshStatus() {
    try {
      const { data } = await axiosInstance.get('/admin/first-run-wizard/status');
      credentialKind = data.credential_kind ?? null;
      minAgeHours = data.password_min_age_hours ?? 0;
      passwordChanged = Boolean(data.password_changed_by_you);
      termsAccepted = Boolean(data.legal_acknowledged);
      if (data.legal_required && !data.legal_acknowledged) {
        includeTerms = true;
        loadTermsDocument();
      } else if (!data.legal_required) {
        includeTerms = false;
      }
      return data;
    } catch (err) {
      // Never block the app over this -- see the endpoint's own docstring.
      console.error('Error checking first-run wizard status:', err);
      return null;
    }
  }

  async function loadTermsDocument() {
    if (termsDoc) return;
    termsLoadFailed = false;
    try {
      termsDoc = await getLegalDocument(OPERATOR_ACK_KEY);
    } catch (err) {
      console.error('Error loading operator acknowledgment:', err);
      termsLoadFailed = true;
    }
  }

  async function acceptTerms() {
    if (!termsDoc || !termsChecked) return;
    saving = true;
    try {
      await acknowledgeLegalDocument(termsDoc);
      termsAccepted = true;
      step = 1;
    } catch (err) {
      console.error('Error recording acknowledgment:', err);
      toastStore.error($t('firstRunWizard.termsAcceptFailed'));
    } finally {
      saving = false;
    }
  }

  /**
   * @param {string} key
   * @param {boolean} [fullscreen]
   */
  function openViewer(key, fullscreen = false) {
    viewerKey = key;
    viewerFullscreen = fullscreen;
    viewerOpen = true;
  }

  onMount(async () => {
    if (!$user || $user.role !== 'super_admin') return;
    const data = await refreshStatus();
    if (data) {
      visible = !data.completed_at;
      if (includeTerms && !termsAccepted) step = 0;
    }
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
    step = includeTerms && !termsAccepted ? 0 : passwordChanged ? 2 : 1;
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
  <div class="wizard-progress" data-testid="wizard-step-indicator" data-step={step + offset} bind:this={progressEl}>
    <p class="wizard-progress-label">
      {$t('firstRunWizard.stepIndicator', { current: step + offset, total: totalSteps })}
    </p>
    <Stepper
      {steps}
      current={step + offset - 1}
      label={$t('firstRunWizard.stepperLabel')}
      doneLabel={$t('firstRunWizard.stepDoneMark')}
    />
  </div>

  {#if step === 0}
    <div class="wizard-step terms-step-compact" data-testid="wizard-terms-step">
      <p>{$t('firstRunWizard.termsIntro')}</p>
      <p class="wizard-hint">{$t('firstRunWizard.termsNoSkip')}</p>

      {#if termsDoc}
        {#if termsDoc.status === 'draft'}
          <div class="wizard-callout wizard-callout-warning wizard-callout-slim" data-testid="terms-draft-badge">
            <strong>{$t('legal.draftBadge')}</strong>
          </div>
        {/if}
        <div class="terms-document">
          <div class="terms-toolbar">
            <span class="terms-toolbar-meta">{$t('legal.viewer.officialText')}</span>
            <button
              type="button"
              class="btn btn-secondary btn-small"
              data-testid="terms-fullscreen"
              on:click={() => openViewer(OPERATOR_ACK_KEY, true)}
            >
              <MenuIcon name="maximize" />
              {$t('legal.viewer.enterFullscreen')}
            </button>
          </div>
          <!-- A focusable region, so the panel scrolls from the keyboard. -->
          <!-- svelte-ignore a11y-no-noninteractive-tabindex -->
          <div class="terms-panel" tabindex="0" role="region" aria-label={termsDoc.title} data-testid="terms-panel">
            <LegalProse html={termsHtml} density="compact" />
          </div>
        </div>
        <div class="wizard-actions">
          <button class="btn btn-secondary" on:click={() => openViewer(TERMS_KEY)}>
            {$t('firstRunWizard.termsReadTerms')}
          </button>
          <button class="btn btn-secondary" on:click={() => openViewer(PRIVACY_KEY)}>
            {$t('firstRunWizard.termsReadPrivacy')}
          </button>
        </div>
      {:else if termsLoadFailed}
        <div class="wizard-callout wizard-callout-warning" role="alert">
          <span>{$t('legal.viewer.loadFailed')}</span>
        </div>
        <div class="wizard-actions">
          <button class="btn btn-secondary" on:click={loadTermsDocument}>
            {$t('firstRunWizard.termsRetry')}
          </button>
        </div>
      {:else}
        <p class="wizard-hint">{$t('common.loading')}</p>
      {/if}
    </div>
  {:else if step === 1}
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
        {#if mailDeliverable !== null}
          <li data-testid="wizard-summary-mail">
            <strong>{$t('firstRunWizard.summaryMailLabel')}:</strong>
            <!-- "Configured", not "working": a wrong host still reads configured. -->
            {mailDeliverable
              ? $t('firstRunWizard.summaryMailConfigured')
              : $t('firstRunWizard.summaryMailMissing')}
          </li>
        {/if}
      </ul>
      <p>{$t('firstRunWizard.summaryNextSteps')}</p>
    </div>
  {/if}

  {#if step > 0 && step < 4}
    <p class="wizard-hint wizard-skip-hint">{$t('firstRunWizard.skipHint')}</p>
  {/if}

  <svelte:fragment slot="footer">
    {#if step === 0}
      <div class="terms-footer" data-testid="terms-footer">
        {#if termsDoc}
          <label class="wizard-checkbox terms-accept">
            <input type="checkbox" bind:checked={termsChecked} data-testid="terms-checkbox" />
            <span dir="ltr">{checkboxText}</span>
          </label>
        {/if}
        <button
          class="btn btn-primary"
          data-testid="terms-accept-button"
          on:click={acceptTerms}
          disabled={saving || !termsChecked || !termsDoc}
        >
          {saving ? $t('firstRunWizard.termsAccepting') : $t('firstRunWizard.termsAcceptButton')}
        </button>
      </div>
    {:else}
    {#if step > 0 && step < 4}
      <button class="wizard-skip-link" on:click={skip} disabled={saving}>
        {$t('firstRunWizard.skipButton')}
      </button>
    {/if}
    <div class="wizard-footer-spacer"></div>
    {#if step > 1 || (step === 1 && includeTerms)}
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
    {/if}
  </svelte:fragment>
</BaseModal>

<LegalDocumentViewer
  isOpen={viewerOpen}
  docKey={viewerKey}
  startFullscreen={viewerFullscreen}
  onClose={() => (viewerOpen = false)}
/>

<style>
  .terms-document {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--bg-secondary);
    overflow: hidden;
  }

  .terms-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 6px 12px;
    padding: 6px 8px;
    padding-inline-start: 16px;
    border-block-end: 1px solid var(--border-color);
    background-color: var(--surface-color);
  }

  .terms-toolbar-meta {
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .terms-panel {
    max-height: 40vh;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 14px 18px;
    color: var(--text-primary);
  }

  .terms-panel:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
  }

  /* Below 768px the dialog is already full screen and scrolls; a second scroll box inside it
     traps the reader, so the panel grows with its text instead. */
  @media (max-width: 768px) {
    .terms-panel {
      max-height: none;
      overflow-y: visible;
    }
  }

  .wizard-step {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .wizard-progress {
    margin-block-end: 20px;
    /* Room above it when a step change scrolls it back into view (the body's own padding). */
    scroll-margin-block-start: 24px;
  }

  .wizard-progress-label {
    margin: 0 0 12px;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
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
    border: 2px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--bg-secondary);
    box-shadow: none;
    color: var(--text-primary);
    cursor: pointer;
  }

  .posture-card.selected {
    border-color: var(--primary-color);
    background-color: rgba(var(--primary-color-rgb), 0.06);
  }

  .posture-card strong {
    font-size: var(--font-size-md);
  }

  .posture-card span {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-regular);
    color: var(--text-secondary);
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

  /* A text link, not a button: the global button rule's surface, radius and shadow must not
     leak onto it (it used to render as a raised white box). */
  .wizard-skip-link {
    padding: 8px 4px;
    border: none;
    border-radius: 6px;
    background: none;
    box-shadow: none;
    color: var(--text-secondary);
    font-size: var(--font-size-md);
    text-decoration: underline;
    text-underline-offset: 0.18em;
    cursor: pointer;
  }

  .wizard-skip-link:hover:not(:disabled) {
    background: none;
    box-shadow: none;
    color: var(--text-primary);
  }

  .wizard-skip-link:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .terms-footer {
    display: flex;
    flex: 1;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 10px 16px;
  }

  .terms-footer .terms-accept {
    flex: 1 1 280px;
    font-size: var(--font-size-sm);
  }

  .terms-footer .terms-accept:focus-within {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 4px;
  }

  .terms-step-compact {
    gap: 10px;
  }

  .wizard-callout-slim {
    padding: 6px 12px;
    font-size: var(--font-size-sm);
  }
</style>
