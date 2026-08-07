<script lang="ts">
  /**
   * Forced TOTP enrolment.
   *
   * Shown when the deployment requires MFA and the account has no second factor
   * yet: /auth/login answers with an enrolment half-token instead of a session,
   * so this is the ONLY way forward — there is no "skip".
   *
   * `mfaToken` is a half-token. It stays in memory (prop only, never storage)
   * and is sent as a bearer header by the store functions; there is no cookie
   * session until verification succeeds.
   *
   * The component is deliberately session-agnostic: pass an empty `mfaToken` and
   * the same UI drives a voluntary enrolment over the cookie session.
   */
  import { createEventDispatcher, onMount } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import {
    setupMfaEnrollment,
    verifyMfaEnrollment,
    type MfaSetupData,
    type MfaEnrollmentError,
  } from '$stores/auth';

  export let mfaToken: string = '';

  // Bound rather than inlined: a literal `{6}` in an attribute collides with
  // Svelte's mustache syntax.
  const CODE_PATTERN = '[0-9]{6}';

  const dispatch = createEventDispatcher<{ complete: void; expired: void; cancel: void }>();

  type Step = 'loading' | 'scanning' | 'codes' | 'unavailable';

  let step: Step = 'loading';
  let setupData: MfaSetupData | null = null;
  let verifyCode = '';
  let verifying = false;
  let showManualEntry = false;
  let backupCodes: string[] = [];
  let acknowledged = false;
  let unavailableMessage = '';

  onMount(() => {
    void loadSetup();
  });

  /** A spent or expired half-token can only be replaced by a fresh login. */
  function handleExpired(error: MfaEnrollmentError) {
    toastStore.error(error.message);
    dispatch('expired');
  }

  async function loadSetup() {
    step = 'loading';
    const result = await setupMfaEnrollment(mfaToken);

    if (result.success) {
      setupData = result.data;
      step = 'scanning';
      return;
    }

    if (result.error.kind === 'expired') {
      handleExpired(result.error);
      return;
    }
    if (result.error.kind === 'unavailable') {
      unavailableMessage = result.error.message;
      step = 'unavailable';
      return;
    }
    // 'restart' at this point means the server already considers MFA enabled;
    // there is nothing to re-run, so send them back for a fresh login.
    handleExpired(result.error);
  }

  async function verify() {
    if (verifyCode.length !== 6) {
      toastStore.error($t('auth.mfaEnroll.codeLength'));
      return;
    }

    verifying = true;
    const result = await verifyMfaEnrollment(verifyCode, mfaToken);
    verifying = false;

    if (result.success) {
      backupCodes = result.backupCodes;
      verifyCode = '';
      step = 'codes';
      return;
    }

    switch (result.error.kind) {
      case 'retry':
        // Half-token survives a mistyped code — stay put and let them retry.
        toastStore.error(result.error.message);
        verifyCode = '';
        break;
      case 'restart':
        toastStore.error(result.error.message);
        void loadSetup();
        break;
      case 'unavailable':
        unavailableMessage = result.error.message;
        step = 'unavailable';
        break;
      default:
        handleExpired(result.error);
    }
  }

  function copyCodes() {
    navigator.clipboard.writeText(backupCodes.join('\n'));
    toastStore.success($t('auth.mfaEnroll.codesCopied'));
  }

  function downloadCodes() {
    const body = backupCodes.map((code, i) => `${i + 1}. ${code}`).join('\n');
    const blob = new Blob([`${$t('auth.mfaEnroll.codesFileHeading')}\n\n${body}\n`], {
      type: 'text/plain',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'opentranscribe-backup-codes.txt';
    link.click();
    URL.revokeObjectURL(url);
  }
</script>

<div class="mfa-enroll">
  {#if step === 'loading'}
    <div class="enroll-loading">
      <Spinner size="small" />
      <p>{$t('auth.mfaEnroll.preparing')}</p>
    </div>
  {:else if step === 'unavailable'}
    <h2>{$t('auth.mfaEnroll.unavailableTitle')}</h2>
    <p class="enroll-lead">{unavailableMessage}</p>
    <button type="button" class="btn btn-secondary" on:click={() => dispatch('cancel')}>
      {$t('auth.mfaEnroll.backToLogin')}
    </button>
  {:else if step === 'scanning' && setupData}
    <h2>{$t('auth.mfaEnroll.title')}</h2>
    <p class="enroll-lead">{$t('auth.mfaEnroll.requiredExplanation')}</p>
    <p class="enroll-instruction">{$t('auth.mfaEnroll.scanInstructions')}</p>

    <div class="qr-code">
      <img
        src="data:image/png;base64,{setupData.qr_code_base64}"
        alt={$t('auth.mfaEnroll.qrCodeAlt')}
      />
    </div>

    <button type="button" class="link-button" on:click={() => (showManualEntry = !showManualEntry)}>
      {showManualEntry ? $t('auth.mfaEnroll.hideManualEntry') : $t('auth.mfaEnroll.showManualEntry')}
    </button>

    {#if showManualEntry}
      <div class="manual-entry">
        <span class="manual-entry-label">{$t('auth.mfaEnroll.secretKey')}</span>
        <code class="secret-code">{setupData.secret}</code>
      </div>
    {/if}

    <form class="verify-form" on:submit|preventDefault={verify}>
      <label for="mfa-enroll-code">{$t('auth.mfaEnroll.enterCode')}</label>
      <input
        type="text"
        id="mfa-enroll-code"
        class="code-input"
        bind:value={verifyCode}
        placeholder="000000"
        maxlength="6"
        pattern={CODE_PATTERN}
        inputmode="numeric"
        autocomplete="one-time-code"
      />
      <button type="submit" class="btn btn-primary" disabled={verifyCode.length !== 6 || verifying}>
        {#if verifying}
          <Spinner size="small" color="white" /> {$t('auth.mfaEnroll.verifying')}
        {:else}
          {$t('auth.mfaEnroll.verifyAndContinue')}
        {/if}
      </button>
    </form>

    <button type="button" class="link-button" on:click={() => dispatch('cancel')}>
      {$t('auth.mfaEnroll.backToLogin')}
    </button>
  {:else if step === 'codes'}
    <h2>{$t('auth.mfaEnroll.backupCodesTitle')}</h2>
    <p class="codes-warning" role="alert">{$t('auth.mfaEnroll.backupCodesWarning')}</p>

    <div class="codes-grid">
      {#each backupCodes as code, i (code)}
        <div class="backup-code">{i + 1}. {code}</div>
      {/each}
    </div>

    <div class="codes-actions">
      <button type="button" class="btn btn-secondary" on:click={copyCodes}>
        {$t('auth.mfaEnroll.copyCodes')}
      </button>
      <button type="button" class="btn btn-secondary" on:click={downloadCodes}>
        {$t('auth.mfaEnroll.downloadCodes')}
      </button>
    </div>

    <label class="ack-label">
      <input type="checkbox" bind:checked={acknowledged} />
      <span>{$t('auth.mfaEnroll.acknowledge')}</span>
    </label>

    <button
      type="button"
      class="btn btn-primary"
      disabled={!acknowledged}
      on:click={() => dispatch('complete')}
    >
      {$t('auth.mfaEnroll.continue')}
    </button>
  {/if}
</div>

<style>
  .mfa-enroll {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    text-align: center;
  }

  .mfa-enroll h2 {
    margin: 0;
    font-size: 1.25rem;
    color: var(--text-color);
  }

  .enroll-loading {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    padding: 1.5rem 0;
  }

  .enroll-loading p,
  .enroll-lead,
  .enroll-instruction {
    margin: 0;
    font-size: 0.875rem;
    color: var(--text-light);
  }

  .enroll-lead {
    padding: 0.75rem;
    border: 1px solid rgba(245, 158, 11, 0.4);
    border-radius: 6px;
    background-color: rgba(245, 158, 11, 0.12);
    color: var(--text-color);
  }

  .qr-code {
    align-self: center;
    padding: 1rem;
    /* The QR must stay dark-on-white in both themes to remain scannable. */
    background-color: #ffffff;
    border-radius: 8px;
  }

  .qr-code img {
    display: block;
    width: 180px;
    height: 180px;
  }

  .link-button {
    align-self: center;
    padding: 0;
    background: none;
    border: none;
    color: var(--primary-color);
    font-size: 0.85rem;
    text-decoration: underline;
    cursor: pointer;
  }

  .manual-entry-label {
    display: block;
    margin-bottom: 0.375rem;
    font-size: 0.75rem;
    color: var(--text-light);
  }

  .secret-code {
    display: block;
    padding: 0.625rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-family: monospace;
    font-size: 0.875rem;
    word-break: break-all;
    user-select: all;
  }

  .verify-form {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    text-align: left;
  }

  .verify-form label {
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .code-input {
    padding: 0.75rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-family: monospace;
    font-size: 1.25rem;
    letter-spacing: 0.25em;
    text-align: center;
  }

  .code-input:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .codes-warning {
    margin: 0;
    padding: 0.75rem;
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-radius: 6px;
    background-color: rgba(245, 158, 11, 0.12);
    color: var(--text-color);
    font-size: 0.85rem;
  }

  .codes-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0.5rem;
  }

  .backup-code {
    padding: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-family: monospace;
    font-size: 0.85rem;
  }

  .codes-actions {
    display: flex;
    gap: 0.75rem;
  }

  .codes-actions .btn {
    flex: 1;
  }

  .ack-label {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    text-align: left;
    font-size: 0.85rem;
    color: var(--text-color);
    cursor: pointer;
  }

  .ack-label input {
    margin-top: 0.15rem;
    flex-shrink: 0;
  }

  @media (max-width: 480px) {
    .codes-grid {
      grid-template-columns: 1fr;
    }

    .codes-actions {
      flex-direction: column;
    }
  }
</style>
