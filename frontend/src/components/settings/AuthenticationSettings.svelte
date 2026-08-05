<script lang="ts">
  import { onMount } from 'svelte';
  import { AuthConfigApi } from '$lib/api/authConfig';
  import LDAPSettings from './LDAPSettings.svelte';
  import KeycloakSettings from './KeycloakSettings.svelte';
  import PKISettings from './PKISettings.svelte';
  import LocalAuthSettings from './LocalAuthSettings.svelte';
  import SessionSettings from './SessionSettings.svelte';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let activeTab = 'local';
  let loading = false;
  let configs: Record<string, any> = {};
  let hasUnsavedChanges = false;
  let backendNotReady = false; // Backend is fully implemented

  $: tabs = [
    { id: 'local', label: $t('settings.authentication.tab.local') },
    { id: 'ldap', label: $t('settings.authentication.tab.ldap') },
    { id: 'keycloak', label: $t('settings.authentication.tab.keycloak') },
    { id: 'pki', label: $t('settings.authentication.tab.pki') },
    { id: 'session', label: $t('settings.authentication.tab.session') }
  ];

  onMount(async () => {
    await loadConfigs();
  });

  // Transform array of config objects to key-value dictionary
  function transformConfigArray(configArray: any[]): Record<string, any> {
    if (!Array.isArray(configArray)) return configArray || {};
    const result: Record<string, any> = {};
    for (const item of configArray) {
      if (item.config_key && item.config_value !== undefined) {
        // Convert string values to appropriate types
        let value = item.config_value;
        if (item.data_type === 'bool') {
          value = value === 'true' || value === true;
        } else if (item.data_type === 'int') {
          value = parseInt(value, 10) || 0;
        }
        result[item.config_key] = value;
      }
    }
    return result;
  }

  async function loadConfigs() {
    loading = true;
    try {
      const allConfigs = await AuthConfigApi.getAllConfigs();
      // Transform each category's array to key-value dictionary
      configs = {};
      for (const [category, configArray] of Object.entries(allConfigs)) {
        configs[category] = transformConfigArray(configArray as any[]);
      }
    } catch (error) {
      console.error('Failed to load auth config:', error);
      toastStore.error($t('settings.authentication.loadError'));
    } finally {
      loading = false;
    }
  }

  async function handleSave(category: string, config: Record<string, any>) {
    try {
      await AuthConfigApi.updateCategory(category, config);
      toastStore.success($t('settings.authentication.configSaved', { category }));
      hasUnsavedChanges = false;
      await loadConfigs();
    } catch (error) {
      console.error(`Failed to save ${category} config:`, error);
      toastStore.error($t('settings.authentication.configSaveFailed', { category }));
    }
  }

  async function handleTestConnection(category: string, config: Record<string, any>) {
    try {
      const result = await AuthConfigApi.testConnection(category, config);
      if (result.success) {
        toastStore.success(result.message);
      } else {
        toastStore.error(result.message);
      }
      return result;
    } catch (error) {
      console.error(`Connection test for ${category} failed:`, error);
      toastStore.error($t('settings.authentication.connectionTestFailed'));
      return { success: false, message: $t('settings.authentication.connectionTestFailed') };
    }
  }

  function handleChange() {
    hasUnsavedChanges = true;
  }
</script>

<div class="auth-settings">
  <div class="settings-header">
    <h2>{$t('settings.authentication.heading')}</h2>
  </div>

  {#if backendNotReady}
    <!-- Database-backed configuration UI - Coming Soon -->
    <div class="coming-soon">
      <div class="coming-soon-icon">🔐</div>
      <h3>{$t('settings.authentication.dbBackedTitle')}</h3>
      <p>{$t('settings.authentication.comingSoonDesc')}</p>

      <div class="config-methods">
        <div class="config-method">
          <h4>{$t('settings.authentication.method.ldap')}</h4>
          <code>LDAP_ENABLED=true</code>
          <p>{$t('settings.authentication.seeDoc')} <a href="https://github.com/davidamacey/OpenTranscribe/blob/main/docs/LDAP_AUTH.md" target="_blank" rel="noopener noreferrer">LDAP_AUTH.md</a></p>
        </div>

        <div class="config-method">
          <h4>{$t('settings.authentication.method.keycloak')}</h4>
          <code>KEYCLOAK_ENABLED=true</code>
          <p>{$t('settings.authentication.seeDoc')} <a href="https://github.com/davidamacey/OpenTranscribe/blob/main/docs/KEYCLOAK_SETUP.md" target="_blank" rel="noopener noreferrer">KEYCLOAK_SETUP.md</a></p>
        </div>

        <div class="config-method">
          <h4>{$t('settings.authentication.method.pki')}</h4>
          <code>PKI_ENABLED=true</code>
          <p>{$t('settings.authentication.seeDoc')} <a href="https://github.com/davidamacey/OpenTranscribe/blob/main/docs/PKI_SETUP.md" target="_blank" rel="noopener noreferrer">PKI_SETUP.md</a></p>
        </div>

        <div class="config-method">
          <h4>{$t('settings.authentication.method.mfa')}</h4>
          <code>MFA_ENABLED=true</code>
          <p>{$t('settings.authentication.seeDoc')} <a href="https://github.com/davidamacey/OpenTranscribe/blob/main/example_env.txt" target="_blank" rel="noopener noreferrer">example_env.txt</a></p>
        </div>
      </div>

      <p class="note">
        <strong>{$t('settings.authentication.noteLabel')}</strong> {$t('settings.authentication.comingSoonNote')}
      </p>
    </div>
  {:else}
    <div class="tabs">
      {#each tabs as tab}
        <button
          class="tab"
          class:active={activeTab === tab.id}
          on:click={() => activeTab = tab.id}
        >
          {tab.label}
        </button>
      {/each}
    </div>

    {#if loading}
      <div class="loading">{$t('settings.authentication.loadingConfig')}</div>
    {:else}
      <div class="tab-content">
        {#if activeTab === 'local'}
          <LocalAuthSettings
            config={configs.local || {}}
            on:save={(e) => handleSave('local', e.detail)}
            on:change={handleChange}
          />
        {:else if activeTab === 'ldap'}
          <LDAPSettings
            config={configs.ldap || {}}
            on:save={(e) => handleSave('ldap', e.detail)}
            on:test={(e) => handleTestConnection('ldap', e.detail)}
            on:change={handleChange}
          />
        {:else if activeTab === 'keycloak'}
          <KeycloakSettings
            config={configs.keycloak || {}}
            on:save={(e) => handleSave('keycloak', e.detail)}
            on:test={(e) => handleTestConnection('keycloak', e.detail)}
            on:change={handleChange}
          />
        {:else if activeTab === 'pki'}
          <PKISettings
            config={configs.pki || {}}
            on:save={(e) => handleSave('pki', e.detail)}
            on:change={handleChange}
          />
        {:else if activeTab === 'session'}
          <SessionSettings
            config={configs.session || {}}
            on:save={(e) => handleSave('session', e.detail)}
            on:change={handleChange}
          />
        {/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .auth-settings {
    padding: 1rem;
  }

  .settings-header {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .settings-header h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
  }

  .tabs {
    display: flex;
    gap: 0.5rem;
    border-bottom: 1px solid var(--color-border);
    margin-bottom: 1rem;
  }

  .tab {
    padding: 0.5rem 1rem;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    cursor: pointer;
    color: var(--color-text-secondary);
    font-size: 0.875rem;
    box-shadow: none;
  }

  .tab:hover {
    color: var(--color-text);
    background: none;
    transform: none;
    box-shadow: none;
  }

  .tab.active {
    color: var(--color-primary);
    border-bottom-color: var(--color-primary);
  }

  .loading {
    text-align: center;
    padding: 2rem;
    color: var(--color-text-secondary);
  }

  .tab-content {
    padding: 1rem 0;
  }

  /* Coming Soon Section Styles */
  .coming-soon {
    text-align: center;
    padding: 2rem;
    background: var(--color-surface);
    border-radius: 8px;
    border: 1px solid var(--color-border);
  }

  .coming-soon-icon {
    font-size: 3rem;
    margin-bottom: 1rem;
  }

  .coming-soon h3 {
    margin: 0 0 0.5rem 0;
    font-size: 1.25rem;
    color: var(--color-text);
  }

  .coming-soon p {
    color: var(--color-text-secondary);
    margin: 0.5rem 0;
  }

  .config-methods {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin: 1.5rem 0;
    text-align: left;
  }

  .config-method {
    background: var(--color-background);
    padding: 1rem;
    border-radius: 6px;
    border: 1px solid var(--color-border);
  }

  .config-method h4 {
    margin: 0 0 0.5rem 0;
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--color-text);
  }

  .config-method code {
    display: block;
    background: var(--color-surface);
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    margin-bottom: 0.5rem;
    color: var(--color-primary);
  }

  .config-method p {
    font-size: 0.75rem;
    margin: 0;
  }

  .config-method a {
    color: var(--color-primary);
    text-decoration: none;
  }

  .config-method a:hover {
    text-decoration: underline;
  }

  .note {
    background: var(--color-info-bg, rgba(var(--primary-color-rgb), 0.1));
    border: 1px solid var(--color-info-border, rgba(var(--primary-color-rgb), 0.3));
    border-radius: 6px;
    padding: 1rem;
    margin-top: 1rem;
    text-align: left;
    font-size: 0.875rem;
  }

  .note strong {
    color: var(--color-info, var(--primary-color));
  }

  @media (max-width: 768px) {
    .tabs {
      flex-wrap: wrap;
    }

    .config-methods {
      grid-template-columns: 1fr;
    }

    .tab {
      min-height: 44px;
      flex: 1;
      text-align: center;
    }
  }
</style>
