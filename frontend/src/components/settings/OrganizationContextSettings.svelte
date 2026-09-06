<script lang="ts">
  import { onMount } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import {
    getOrganizationContext,
    updateOrganizationContext,
    resetOrganizationContext,
    getSharedOrganizationContexts,
    useSharedOrganizationContext,
    type SharedOrganizationContext,
  } from '$lib/api/organizationContext';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let contextText = '';
  let includeInDefaultPrompts = true;
  let includeInCustomPrompts = false;
  let isShared = false;
  let usingSharedFrom: string | null = null;
  let loading = false;
  let saving = false;

  // Shared contexts from other users
  let sharedContexts: SharedOrganizationContext[] = [];

  // Original values for change detection
  let originalContextText = '';
  let originalIncludeInDefaultPrompts = true;
  let originalIncludeInCustomPrompts = false;
  let originalIsShared = false;

  $: settingsChanged =
    contextText !== originalContextText ||
    includeInDefaultPrompts !== originalIncludeInDefaultPrompts ||
    includeInCustomPrompts !== originalIncludeInCustomPrompts;

  $: {
    settingsModalStore.setDirty('organization-context', settingsChanged);
  }

  $: charCount = contextText.length;
  $: charLimitWarning = charCount > 9000;

  onMount(async () => {
    await loadSettings();
  });

  async function loadSettings() {
    loading = true;
    try {
      const [settings, shared] = await Promise.all([
        getOrganizationContext(),
        getSharedOrganizationContexts(),
      ]);

      contextText = settings.context_text;
      includeInDefaultPrompts = settings.include_in_default_prompts;
      includeInCustomPrompts = settings.include_in_custom_prompts;
      isShared = settings.is_shared;
      usingSharedFrom = settings.using_shared_from;

      originalContextText = settings.context_text;
      originalIncludeInDefaultPrompts = settings.include_in_default_prompts;
      originalIncludeInCustomPrompts = settings.include_in_custom_prompts;
      originalIsShared = settings.is_shared;

      sharedContexts = shared.shared_contexts || [];
    } catch (err) {
      console.error('Failed to load organization context:', err);
      toastStore.error($t('settings.orgContext.loadFailed'));
    } finally {
      loading = false;
    }
  }

  async function saveSettings() {
    saving = true;
    try {
      const result = await updateOrganizationContext({
        context_text: contextText,
        include_in_default_prompts: includeInDefaultPrompts,
        include_in_custom_prompts: includeInCustomPrompts,
      });

      contextText = result.context_text;
      includeInDefaultPrompts = result.include_in_default_prompts;
      includeInCustomPrompts = result.include_in_custom_prompts;

      originalContextText = result.context_text;
      originalIncludeInDefaultPrompts = result.include_in_default_prompts;
      originalIncludeInCustomPrompts = result.include_in_custom_prompts;

      settingsModalStore.clearDirty('organization-context');
      toastStore.success($t('settings.orgContext.saved'));
    } catch (err) {
      console.error('Failed to save organization context:', err);
      toastStore.error($t('settings.orgContext.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function handleReset() {
    saving = true;
    try {
      await resetOrganizationContext();

      contextText = '';
      includeInDefaultPrompts = true;
      includeInCustomPrompts = false;

      originalContextText = '';
      originalIncludeInDefaultPrompts = true;
      originalIncludeInCustomPrompts = false;

      settingsModalStore.clearDirty('organization-context');
      toastStore.success($t('settings.orgContext.resetSuccess'));
    } catch (err) {
      console.error('Failed to reset organization context:', err);
      toastStore.error($t('settings.orgContext.resetFailed'));
    } finally {
      saving = false;
    }
  }

  async function handleShareToggle() {
    const newShared = !isShared;
    const prevShared = isShared;
    isShared = newShared;

    try {
      await updateOrganizationContext({ is_shared: newShared });
      originalIsShared = newShared;
      toastStore.success(
        newShared ? $t('settings.orgContext.shareEnabled') : $t('settings.orgContext.shareDisabled'),
        3000
      );
    } catch (err) {
      isShared = prevShared;
      console.error('Failed to toggle sharing:', err);
      toastStore.error($t('settings.orgContext.shareFailed'));
    }
  }

  async function handleUseShared(sharedUserId: string) {
    saving = true;
    try {
      const result = await useSharedOrganizationContext(sharedUserId);
      usingSharedFrom = result.using_shared_from;
      // Update the active state in the local list
      sharedContexts = sharedContexts.map(sc => ({
        ...sc,
        is_active: sc.user_id === sharedUserId,
      }));
      toastStore.success($t('settings.orgContext.usingShared'), 3000);
    } catch (err) {
      console.error('Failed to use shared context:', err);
      toastStore.error($t('settings.orgContext.useSharedFailed'));
    } finally {
      saving = false;
    }
  }

  async function handleStopUsingShared() {
    saving = true;
    try {
      const result = await useSharedOrganizationContext(null);
      usingSharedFrom = result.using_shared_from;
      sharedContexts = sharedContexts.map(sc => ({ ...sc, is_active: false }));
      toastStore.success($t('settings.orgContext.stoppedUsingShared'), 3000);
    } catch (err) {
      console.error('Failed to stop using shared context:', err);
      toastStore.error($t('settings.orgContext.useSharedFailed'));
    } finally {
      saving = false;
    }
  }
</script>

<div class="org-context-settings">
  {#if loading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.orgContext.loading')}</p>
    </div>
  {:else}
    <div class="settings-form">
      <!-- Using shared context banner -->
      {#if usingSharedFrom}
        {@const activeShared = sharedContexts.find(sc => sc.user_id === usingSharedFrom)}
        {#if activeShared}
          <div class="using-shared-banner">
            <div class="banner-content">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
              </svg>
              <span>{$t('settings.orgContext.usingSharedBanner', { name: activeShared.owner_name })}</span>
            </div>
            <button class="btn-stop-shared" on:click={handleStopUsingShared} disabled={saving}>
              {$t('settings.orgContext.useOwn')}
            </button>
          </div>
        {/if}
      {/if}

      <!-- Context Text -->
      <div class="form-group">
        <label for="org-context-text" class="form-label">
          {$t('settings.orgContext.label')}
          <span class="label-hint">{$t('settings.orgContext.labelHint')}</span>
        </label>
        <textarea
          id="org-context-text"
          class="form-textarea"
          bind:value={contextText}
          maxlength="10000"
          rows="8"
          placeholder={$t('settings.orgContext.placeholder')}
        ></textarea>
        <div class="char-counter" class:char-warning={charLimitWarning}>
          {charCount.toLocaleString()} / 10,000
        </div>
      </div>

      <!-- Include in Default Prompts -->
      <div class="form-group">
        <div class="toggle-group">
          <div class="toggle-header">
            <label for="include-default" class="toggle-label">
              <span class="label-text">{$t('settings.orgContext.includeDefault')}</span>
              <span class="label-description">
                {$t('settings.orgContext.includeDefaultDesc')}
              </span>
            </label>
            <label class="toggle-switch">
              <input
                id="include-default"
                type="checkbox"
                bind:checked={includeInDefaultPrompts}
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>

      <!-- Include in Custom Prompts -->
      <div class="form-group">
        <div class="toggle-group">
          <div class="toggle-header">
            <label for="include-custom" class="toggle-label">
              <span class="label-text">{$t('settings.orgContext.includeCustom')}</span>
              <span class="label-description">
                {$t('settings.orgContext.includeCustomDesc')}
              </span>
            </label>
            <label class="toggle-switch">
              <input
                id="include-custom"
                type="checkbox"
                bind:checked={includeInCustomPrompts}
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>

      <!-- Share with all users -->
      <div class="form-group">
        <div class="toggle-group">
          <div class="toggle-header">
            <label for="share-context" class="toggle-label">
              <span class="label-text">{$t('settings.orgContext.shareGlobally')}</span>
              <span class="label-description">
                {$t('settings.orgContext.shareDescription')}
              </span>
            </label>
            <label class="toggle-switch">
              <input
                id="share-context"
                type="checkbox"
                checked={isShared}
                on:change={handleShareToggle}
                disabled={!contextText.trim()}
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>

      <!-- Actions -->
      <div class="form-actions">
        <button
          class="btn btn-secondary"
          on:click={handleReset}
          disabled={saving}
        >
          {$t('common.resetToDefaults')}
        </button>
        <button
          class="btn btn-primary"
          on:click={saveSettings}
          disabled={saving || !settingsChanged}
        >
          {#if saving}
            <Spinner size="small" color="white" />
            {$t('common.saving')}
          {:else}
            {$t('common.saveSettings')}
          {/if}
        </button>
      </div>

      <!-- Shared by Others -->
      {#if sharedContexts.length > 0}
        <div class="shared-section">
          <h4 class="shared-section-title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
            </svg>
            {$t('settings.orgContext.sharedByOthers')}
          </h4>
          {#each sharedContexts as shared}
            <div class="shared-card" class:active={shared.is_active}>
              <div class="shared-card-header">
                <div class="shared-card-info">
                  <span class="shared-owner">{shared.owner_name}</span>
                  {#if shared.owner_role === 'admin' || shared.owner_role === 'super_admin'}
                    <span class="admin-badge">{$t('settings.sharing.adminBadge')}</span>
                  {/if}
                  {#if shared.is_active}
                    <span class="active-badge">{$t('settings.orgContext.activeLabel')}</span>
                  {/if}
                </div>
                <div class="shared-card-actions">
                  {#if shared.is_active}
                    <button
                      class="btn-shared-action btn-stop"
                      on:click={handleStopUsingShared}
                      disabled={saving}
                    >
                      {$t('settings.orgContext.useOwn')}
                    </button>
                  {:else}
                    <button
                      class="btn-shared-action btn-use"
                      on:click={() => handleUseShared(shared.user_id)}
                      disabled={saving}
                    >
                      {$t('settings.orgContext.useThis')}
                    </button>
                  {/if}
                </div>
              </div>
              <div class="shared-card-preview">
                {shared.context_text.length > 200
                  ? shared.context_text.substring(0, 200) + '...'
                  : shared.context_text}
              </div>
            </div>
          {/each}
        </div>
      {/if}

      <!-- Info Box -->
      <div class="info-box">
        <div class="info-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="12" y1="16" x2="12" y2="12"></line>
            <line x1="12" y1="8" x2="12.01" y2="8"></line>
          </svg>
        </div>
        <div class="info-content">
          <p><strong>{$t('settings.orgContext.howItWorks')}</strong></p>
          <ul>
            <li>{$t('settings.orgContext.howItWorks1')}</li>
            <li>{$t('settings.orgContext.howItWorks2')}</li>
            <li>{$t('settings.orgContext.howItWorks3')}</li>
            <li>{$t('settings.orgContext.howItWorks4')}</li>
          </ul>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .org-context-settings {
    max-width: 800px;
    padding: 1.5rem 0;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem;
    gap: 1rem;
    color: var(--text-secondary);
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .form-label {
    font-weight: 500;
    color: var(--text-color);
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .label-hint {
    font-size: 0.8125rem;
    font-weight: 400;
    color: var(--text-secondary);
  }

  .form-textarea {
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: 0.8125rem;
    font-family: inherit;
    background: var(--background-color);
    color: var(--text-color);
    resize: vertical;
    min-height: 120px;
    transition: all 0.2s ease;
    line-height: 1.5;
  }

  .form-textarea:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(var(--primary-color-rgb), 0.1);
  }

  .form-textarea::placeholder {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .char-counter {
    font-size: 0.75rem;
    color: var(--text-secondary);
    text-align: right;
  }

  .char-warning {
    color: var(--warning-color, #f59e0b);
  }

  .toggle-group {
    padding: 1rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    transition: all 0.2s ease;
  }

  .toggle-group:hover {
    border-color: var(--primary-color);
  }

  .toggle-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }

  .toggle-label {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    cursor: pointer;
  }

  .label-text {
    font-weight: 500;
    color: var(--text-color);
  }

  .label-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .toggle-switch {
    position: relative;
    display: inline-block;
    width: 48px;
    height: 24px;
    flex-shrink: 0;
    cursor: pointer;
  }

  .toggle-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    cursor: pointer;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: var(--border-color);
    transition: 0.3s;
    border-radius: 24px;
  }

  .toggle-slider:before {
    position: absolute;
    content: "";
    height: 18px;
    width: 18px;
    left: 3px;
    bottom: 3px;
    background-color: white;
    transition: 0.3s;
    border-radius: 50%;
  }

  input:checked + .toggle-slider {
    background-color: var(--primary-color);
  }

  input:checked + .toggle-slider:before {
    transform: translateX(24px);
  }

  .form-actions {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
    padding-top: 0.5rem;
  }

  .btn-secondary {
    margin-right: auto;
  }

  /* Using shared banner */
  .using-shared-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: rgba(var(--primary-color-rgb), 0.08);
    border: 1px solid rgba(var(--primary-color-rgb), 0.2);
    border-radius: 8px;
  }

  .banner-content {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--primary-on-surface);
    font-size: 0.8125rem;
    font-weight: 500;
  }

  :global([data-theme='dark']) .banner-content {
    color: var(--primary-on-surface);
  }

  .btn-stop-shared {
    padding: 0.375rem 0.75rem;
    background-color: #ef4444;
    border: none;
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    color: white;
    transition: all 0.15s;
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .btn-stop-shared:hover:not(:disabled) {
    background-color: #dc2626;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.3);
  }

  .btn-stop-shared:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-stop-shared:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Shared by Others section */
  .shared-section {
    margin-top: 0.5rem;
    padding-top: 1rem;
    border-top: 1px dashed var(--border-color);
  }

  .shared-section-title {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0 0 0.75rem 0;
  }

  .shared-card {
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    margin-bottom: 0.75rem;
    transition: all 0.2s;
  }

  .shared-card.active {
    border-color: var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.04);
  }

  :global([data-theme='dark']) .shared-card.active {
    background: rgba(var(--primary-color-rgb), 0.06);
  }

  .shared-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }

  .shared-card-info {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .shared-owner {
    font-weight: 500;
    font-size: 0.875rem;
    color: var(--text-color);
  }

  .admin-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 10px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    background: rgba(245, 158, 11, 0.12);
    color: #d97706;
  }

  :global([data-theme='dark']) .admin-badge {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  .active-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 10px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    background: rgba(16, 185, 129, 0.12);
    color: #10b981;
  }

  .shared-card-preview {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .shared-card-actions {
    flex-shrink: 0;
  }

  .btn-shared-action {
    padding: 0.375rem 0.75rem;
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
  }

  .btn-use {
    background-color: var(--primary-color);
    color: white;
    border: none;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-use:hover:not(:disabled) {
    background-color: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-use:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-stop {
    background-color: #ef4444;
    color: white;
    border: none;
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .btn-stop:hover:not(:disabled) {
    background-color: #dc2626;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.3);
  }

  .btn-stop:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-shared-action:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Info box */
  .info-box {
    display: flex;
    gap: 1rem;
    padding: 1rem;
    background: rgba(var(--primary-color-rgb), 0.05);
    border: 1px solid rgba(var(--primary-color-rgb), 0.2);
    border-radius: 8px;
    margin-top: 1rem;
  }

  .info-icon {
    flex-shrink: 0;
    color: var(--primary-on-surface);
  }

  .info-content {
    flex: 1;
  }

  .info-content p {
    margin: 0 0 0.5rem 0;
    color: var(--text-color);
  }

  .info-content ul {
    margin: 0;
    padding-left: 1.25rem;
    color: var(--text-secondary);
    font-size: 0.8125rem;
    line-height: 1.6;
  }

  .info-content li {
    margin-bottom: 0.25rem;
  }

  /* Dark mode */
  :global([data-theme='dark']) .toggle-slider:before {
    background-color: #e5e7eb;
  }

  /* Responsive */
  @media (max-width: 640px) {
    .org-context-settings {
      padding: 1rem 0;
    }

    .toggle-header {
      flex-direction: column;
    }

    .form-actions {
      flex-direction: column;
    }

    .btn {
      width: 100%;
      justify-content: center;
      min-height: 44px;
    }

    .info-box {
      flex-direction: column;
    }

    .using-shared-banner {
      flex-direction: column;
      align-items: flex-start;
    }

    .shared-card-header {
      flex-direction: column;
      align-items: flex-start;
      gap: 0.5rem;
    }
  }
</style>
