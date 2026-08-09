<script lang="ts">
  import { onMount } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { copyToClipboard } from '$lib/utils/clipboard';
  import { getAuthMethods, user as userStore } from '$stores/auth';
  import CertificateInfo from './CertificateInfo.svelte';

  // MFA Status
  interface MFAStatus {
    mfa_enabled: boolean;
    mfa_configured: boolean;
    mfa_required: boolean;
    can_setup_mfa: boolean;
  }

  // MFA Setup response
  interface MFASetupResponse {
    secret: string;
    provisioning_uri: string;
    qr_code_base64: string;
  }

  // Component state
  let mfaStatus: MFAStatus | null = null;
  let loading = true;
  let mfaGloballyEnabled = false;

  // Setup flow state
  let setupStep: 'idle' | 'scanning' | 'verifying' | 'complete' = 'idle';
  let setupData: MFASetupResponse | null = null;
  let verifyCode = '';
  let verifyLoading = false;
  let backupCodes: string[] = [];
  let showManualEntry = false;

  // Disable flow state
  let showDisableConfirm = false;
  let disableCode = '';
  let disableLoading = false;

  // PKI user detection
  $: isPKIUser = $userStore?.auth_type === 'pki' && !$userStore?.allow_local_fallback;

  onMount(async () => {
    await loadMFAStatus();
  });

  async function loadMFAStatus() {
    loading = true;
    try {
      // Check if MFA is globally enabled
      const authMethods = await getAuthMethods();
      mfaGloballyEnabled = authMethods.mfa_enabled;

      if (!mfaGloballyEnabled) {
        loading = false;
        return;
      }

      // Get user's MFA status
      const response = await axiosInstance.get('/auth/mfa/status');
      mfaStatus = response.data;
    } catch (err: unknown) {
      console.error('Error loading MFA status:', err);
      toastStore.error($t('settings.security.loadFailed'));
    } finally {
      loading = false;
    }
  }

  async function startMFASetup() {
    setupStep = 'scanning';
    try {
      const response = await axiosInstance.post('/auth/mfa/setup');
      setupData = response.data;
    } catch (err: unknown) {
      console.error('Error starting MFA setup:', err);
      toastStore.error(getErrorMessage(err, $t('settings.security.setupFailed')));
      setupStep = 'idle';
    }
  }

  async function verifyMFASetup() {
    if (verifyCode.length !== 6) {
      toastStore.error($t('settings.security.codeLength'));
      return;
    }

    verifyLoading = true;
    try {
      const response = await axiosInstance.post('/auth/mfa/verify-setup', {
        code: verifyCode
      });

      backupCodes = response.data.backup_codes;
      setupStep = 'complete';
      toastStore.success($t('settings.security.setupSuccess'));
    } catch (err: unknown) {
      console.error('Error verifying MFA setup:', err);
      toastStore.error(getErrorMessage(err, $t('settings.security.verifyFailed')));
    } finally {
      verifyLoading = false;
    }
  }

  function finishSetup() {
    setupStep = 'idle';
    setupData = null;
    verifyCode = '';
    backupCodes = [];
    showManualEntry = false;
    loadMFAStatus();
  }

  async function disableMFA() {
    if (disableCode.length < 6) {
      toastStore.error($t('settings.security.codeLength'));
      return;
    }

    disableLoading = true;
    try {
      await axiosInstance.post('/auth/mfa/disable', {
        code: disableCode
      });

      toastStore.success($t('settings.security.disableSuccess'));
      showDisableConfirm = false;
      disableCode = '';
      await loadMFAStatus();
    } catch (err: unknown) {
      console.error('Error disabling MFA:', err);
      toastStore.error(getErrorMessage(err, $t('settings.security.disableFailed')));
    } finally {
      disableLoading = false;
    }
  }

  function cancelSetup() {
    setupStep = 'idle';
    setupData = null;
    verifyCode = '';
    showManualEntry = false;
  }

  function cancelDisable() {
    showDisableConfirm = false;
    disableCode = '';
  }

  function copyBackupCodes() {
    const codesText = backupCodes.join('\n');
    copyToClipboard(codesText, () => toastStore.success($t('settings.security.codesCopied')));
  }

  function downloadBackupCodes() {
    const codesText = `OpenTranscribe MFA Backup Codes\n${'='.repeat(35)}\n\n${backupCodes.map((code, i) => `${i + 1}. ${code}`).join('\n')}\n\nKeep these codes secure. Each code can only be used once.`;
    const blob = new Blob([codesText], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'opentranscribe-backup-codes.txt';
    a.click();
    URL.revokeObjectURL(url);
  }
</script>

<div class="security-settings">
  <!-- Certificate Info for PKI Users -->
  {#if isPKIUser}
    <CertificateInfo />
  {/if}

  {#if loading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.security.loading')}</p>
    </div>
  {:else if !mfaGloballyEnabled}
    <div class="mfa-disabled-notice">
      <div class="notice-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="8" x2="12" y2="12"></line>
          <line x1="12" y1="16" x2="12.01" y2="16"></line>
        </svg>
      </div>
      <div class="notice-content">
        <h4>{$t('settings.security.mfaNotAvailable')}</h4>
        <p>{$t('settings.security.mfaNotAvailableDesc')}</p>
      </div>
    </div>
  {:else if mfaStatus}
    <!-- MFA Status Section -->
    <div class="mfa-section">
      <div class="mfa-status-card" class:enabled={mfaStatus.mfa_configured}>
        <div class="status-icon" class:enabled={mfaStatus.mfa_configured}>
          {#if mfaStatus.mfa_configured}
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
              <polyline points="9 12 12 15 16 10"></polyline>
            </svg>
          {:else}
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
            </svg>
          {/if}
        </div>
        <div class="status-content">
          <h4>{mfaStatus.mfa_configured ? $t('settings.security.mfaEnabled') : $t('settings.security.mfaDisabled')}</h4>
          <p>{mfaStatus.mfa_configured ? $t('settings.security.mfaEnabledDesc') : $t('settings.security.mfaDisabledDesc')}</p>
        </div>
      </div>

      {#if !mfaStatus.can_setup_mfa}
        <div class="info-notice">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="12" y1="16" x2="12" y2="12"></line>
            <line x1="12" y1="8" x2="12.01" y2="8"></line>
          </svg>
          <span>{$t('settings.security.mfaNotNeeded')}</span>
        </div>
      {:else if !mfaStatus.mfa_configured && setupStep === 'idle'}
        <!-- Setup MFA Button -->
        <button class="btn btn-primary" on:click={startMFASetup}>
          {$t('settings.security.enableMFA')}
        </button>
      {:else if mfaStatus.mfa_configured && !showDisableConfirm}
        <!-- Disable MFA Button -->
        <button class="btn btn-danger" on:click={() => showDisableConfirm = true}>
          {$t('settings.security.disableMFA')}
        </button>
      {/if}
    </div>

    <!-- Setup Flow -->
    {#if setupStep === 'scanning' && setupData}
      <div class="setup-section">
        <h4>{$t('settings.security.setupTitle')}</h4>
        <p class="setup-instruction">{$t('settings.security.setupInstructions')}</p>

        <div class="qr-section">
          <div class="qr-code">
            <img src="data:image/png;base64,{setupData.qr_code_base64}" alt={$t('settings.security.qrCodeAlt')} />
          </div>

          <button class="btn-link" on:click={() => showManualEntry = !showManualEntry}>
            {showManualEntry ? $t('settings.security.hideManualEntry') : $t('settings.security.showManualEntry')}
          </button>

          {#if showManualEntry}
            <div class="manual-entry">
              <span class="manual-entry-label">{$t('settings.security.secretKey')}</span>
              <code class="secret-code">{setupData.secret}</code>
            </div>
          {/if}
        </div>

        <div class="verify-section">
          <label for="verify-code">{$t('settings.security.enterCode')}</label>
          <div class="code-input-group">
            <input
              type="text"
              id="verify-code"
              class="form-control code-input"
              bind:value={verifyCode}
              placeholder="000000"
              maxlength="6"
              pattern="[0-9]*"
              inputmode="numeric"
              autocomplete="one-time-code"
            />
            <button
              class="btn btn-primary"
              on:click={verifyMFASetup}
              disabled={verifyCode.length !== 6 || verifyLoading}
            >
              {verifyLoading ? $t('settings.security.verifying') : $t('settings.security.verify')}
            </button>
          </div>
        </div>

        <button class="btn btn-secondary" on:click={cancelSetup}>
          {$t('common.cancel')}
        </button>
      </div>
    {/if}

    <!-- Backup Codes Display -->
    {#if setupStep === 'complete' && backupCodes.length > 0}
      <div class="backup-codes-section">
        <div class="backup-header">
          <h4>{$t('settings.security.backupCodesTitle')}</h4>
          <span class="warning-badge">{$t('settings.security.saveTheseNow')}</span>
        </div>
        <p class="backup-warning">{$t('settings.security.backupCodesWarning')}</p>

        <div class="backup-codes-grid">
          {#each backupCodes as code, i}
            <div class="backup-code">{i + 1}. {code}</div>
          {/each}
        </div>

        <div class="backup-actions">
          <button class="btn btn-primary" on:click={copyBackupCodes}>
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
            {$t('settings.security.copyCodes')}
          </button>
          <button class="btn btn-primary" on:click={downloadBackupCodes}>
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="7 10 12 15 17 10"></polyline>
              <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            {$t('settings.security.downloadCodes')}
          </button>
        </div>

        <button class="btn btn-primary" on:click={finishSetup}>
          {$t('settings.security.done')}
        </button>
      </div>
    {/if}

    <!-- Disable Confirmation -->
    {#if showDisableConfirm}
      <div class="disable-section">
        <h4>{$t('settings.security.disableTitle')}</h4>
        <p class="disable-warning">{$t('settings.security.disableWarning')}</p>

        <div class="code-input-group">
          <input
            type="text"
            class="form-control code-input"
            bind:value={disableCode}
            placeholder="000000"
            maxlength="8"
            pattern="[0-9A-Za-z]*"
            inputmode="text"
            autocomplete="one-time-code"
          />
          <button
            class="btn btn-danger"
            on:click={disableMFA}
            disabled={disableCode.length < 6 || disableLoading}
          >
            {disableLoading ? $t('settings.security.disabling') : $t('settings.security.confirmDisable')}
          </button>
        </div>

        <button class="btn btn-secondary" on:click={cancelDisable}>
          {$t('common.cancel')}
        </button>
      </div>
    {/if}
  {/if}
</div>

<style>
  .security-settings {
    max-width: 600px;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 2rem;
    color: var(--text-secondary);
  }

  .loading-state p {
    margin: 0;
    font-size: 0.875rem;
  }

  .mfa-disabled-notice {
    display: flex;
    gap: 1rem;
    padding: 1rem;
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .notice-icon {
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .notice-content h4 {
    margin: 0 0 0.25rem 0;
    font-size: 0.9375rem;
    color: var(--text-color);
  }

  .notice-content p {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
  }

  .mfa-section {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .mfa-status-card {
    display: flex;
    gap: 1rem;
    padding: 1rem;
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .mfa-status-card.enabled {
    border-color: var(--success-color, #10b981);
    background-color: rgba(16, 185, 129, 0.05);
  }

  .status-icon {
    width: 48px;
    height: 48px;
    display: flex;
    align-items: center;
    justify-content: center;
    background-color: var(--surface-color);
    border-radius: 50%;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .status-icon.enabled {
    background-color: rgba(16, 185, 129, 0.1);
    color: var(--success-color, #10b981);
  }

  .status-content h4 {
    margin: 0 0 0.25rem 0;
    font-size: 0.9375rem;
    color: var(--text-color);
  }

  .status-content p {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
  }

  .info-notice {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    background-color: rgba(var(--primary-color-rgb), 0.1);
    border-radius: 6px;
    color: var(--primary-color);
    font-size: 0.8125rem;
  }

  .setup-section, .disable-section, .backup-codes-section {
    margin-top: 1.5rem;
    padding: 1.5rem;
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .setup-section h4, .disable-section h4 {
    margin: 0 0 0.5rem 0;
    font-size: 1rem;
    color: var(--text-color);
  }

  .setup-instruction {
    margin: 0 0 1rem 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
  }

  .qr-section {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1rem;
    margin-bottom: 1.5rem;
  }

  .qr-code {
    padding: 1rem;
    background-color: white;
    border-radius: 8px;
  }

  .qr-code img {
    display: block;
    width: 200px;
    height: 200px;
  }

  .btn-link {
    background: none;
    border: none;
    color: var(--primary-color);
    cursor: pointer;
    font-size: 0.8125rem;
    text-decoration: underline;
    padding: 0;
  }

  .btn-link:hover {
    color: var(--primary-hover);
  }

  .manual-entry {
    width: 100%;
    text-align: center;
  }

  .manual-entry-label {
    display: block;
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-bottom: 0.5rem;
  }

  .secret-code {
    display: block;
    padding: 0.75rem 1rem;
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-family: monospace;
    font-size: 0.875rem;
    word-break: break-all;
    user-select: all;
  }

  .verify-section {
    margin-bottom: 1rem;
  }

  .verify-section label {
    display: block;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
    margin-bottom: 0.5rem;
  }

  .code-input-group {
    display: flex;
    gap: 0.75rem;
    margin-bottom: 1rem;
  }

  .code-input {
    flex: 1;
    max-width: 200px;
    text-align: center;
    font-size: 1.25rem;
    font-family: monospace;
    letter-spacing: 0.25em;
  }

  .disable-warning {
    margin: 0 0 1rem 0;
    font-size: 0.8125rem;
    color: var(--error-color);
  }

  .backup-codes-section {
    background-color: rgba(245, 158, 11, 0.05);
    border-color: rgba(245, 158, 11, 0.3);
  }

  .backup-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
  }

  .backup-header h4 {
    margin: 0;
    font-size: 1rem;
    color: var(--text-color);
  }

  .warning-badge {
    padding: 0.25rem 0.5rem;
    background-color: var(--warning-color, #f59e0b);
    color: white;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    border-radius: 4px;
  }

  .backup-warning {
    margin: 0 0 1rem 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
  }

  .backup-codes-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0.5rem;
    margin-bottom: 1rem;
  }

  .backup-code {
    padding: 0.5rem 0.75rem;
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-family: monospace;
    font-size: 0.875rem;
    text-align: center;
  }

  .backup-actions {
    display: flex;
    gap: 0.75rem;
    margin-bottom: 1rem;
  }

  .backup-actions .btn {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .form-control {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
  }

  @media (max-width: 480px) {
    .backup-codes-grid {
      grid-template-columns: 1fr;
    }

    .backup-actions {
      flex-direction: column;
    }

    .code-input-group {
      flex-direction: column;
    }

    .code-input {
      max-width: none;
    }

    .backup-actions button {
      min-height: 44px;
    }
  }

  @media (max-width: 768px) {
    .mfa-section button {
      min-height: 44px;
    }
  }
</style>
