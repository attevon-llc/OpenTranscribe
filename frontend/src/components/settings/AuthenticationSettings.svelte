<script lang="ts">
  import { onMount } from 'svelte';
  import { AuthConfigApi, type AuthMethodTestResponse } from '$lib/api/authConfig';
  import { getEmailConfigs, type EmailConfig } from '$lib/api/watchSourcesApi';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import Tabs from '$components/ui/Tabs.svelte';
  import { firstRunWizardStore } from '$stores/firstRunWizardStore';
  import LazySection from './LazySection.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { authTabLoaders } from '$lib/settings/sectionLoaders';
  import {
    invalidateSectionData,
    loadSectionData,
    peekSectionData,
    sameSectionData,
    SectionDataKey,
  } from '$lib/settings/sectionData';

  let activeTab = 'local';
  // True until the first read of the configuration finishes. Reloads after a save do not set it
  // again: blanking the form (and re-fetching what it fetches on mount) for every save is the
  // slowness this replaced. Starts true so the Local form is never mounted with coded defaults
  // while the stored values are still on the way.
  let configsReady = false;
  let configs: Record<string, any> = {};
  let hasUnsavedChanges = false;
  let policyRefresh = 0;
  let backendNotReady = false; // Backend is fully implemented

  /**
   * The Local tab renders one form, but its fields belong to FOUR backend
   * categories. `PUT /admin/auth-config/{category}` validates against a
   * per-category schema and 400s on unknown keys, so the whole form cannot be
   * PUT to `local`.
   *
   * This is also a correctness fix, not just an accommodation:
   * `password_require_digit`, `mfa_issuer_name` and the `account_lockout_*` pair
   * were never in `local`, so sending them there filed them under a category
   * their own tab could not read.
   *
   * Deliberately omitted: the legacy aliases `password_require_numbers`,
   * `mfa_issuer`, `max_login_attempts` and `lockout_duration_minutes`. The
   * `local` schema still accepts them, but nothing on the backend reads them.
   */
  const LOCAL_TAB_CATEGORY_KEYS: Record<string, string[]> = {
    // `require_email_verification` and `require_account_approval` both live in
    // `local` beside `allow_registration` — together the four are "who may get an
    // account here". Both were enforced by the backend with no control anywhere in
    // the product, so neither could be turned on.
    local: [
      'local_enabled',
      'allow_registration',
      'require_email_verification',
      'require_account_approval'
    ],
    password_policy: [
      'password_min_length',
      'password_require_uppercase',
      'password_require_lowercase',
      'password_require_digit',
      'password_require_special',
      'password_max_age_days',
      'password_history_count',
      'password_policy_profile',
      'password_max_length',
      'password_min_age_hours',
      'password_blocklist_enabled',
      'password_hibp_enabled'
    ],
    mfa: [
      'mfa_enabled',
      'mfa_required',
      'mfa_required_for_admins',
      'mfa_issuer_name',
      'mfa_backup_code_count',
      'mfa_token_expire_minutes'
    ],
    lockout: [
      'account_lockout_enabled',
      'account_lockout_threshold',
      'account_lockout_duration_minutes',
      'account_lockout_progressive',
      'account_lockout_max_duration_minutes'
    ],
    // FedRAMP AC-8 — enforced at login (`get_current_active_user` refuses until
    // acknowledged), not just displayed, so it belongs beside the other controls
    // that gate access rather than off in its own tab for 3 fields.
    banner: ['login_banner_enabled', 'login_banner_text', 'login_banner_classification']
  };

  /**
   * Flatten the Local tab's four source categories into the single object the
   * panel renders. Loading only `configs.local` would paint coded defaults over
   * everything the other three categories have stored.
   *
   * Picking by key (rather than spreading whole categories) also keeps stale
   * alias rows still sitting in `local` from reappearing in the form.
   */
  function collectLocalTabConfig(all: Record<string, any>): Record<string, any> {
    const merged: Record<string, any> = {};
    for (const [category, keys] of Object.entries(LOCAL_TAB_CATEGORY_KEYS)) {
      const stored = all[category] || {};
      for (const key of keys) {
        if (stored[key] !== undefined) merged[key] = stored[key];
      }
    }
    return merged;
  }

  $: localTabConfig = collectLocalTabConfig(configs);

  $: tabs = [
    { id: 'local', label: $t('settings.authentication.tab.local') },
    { id: 'ldap', label: $t('settings.authentication.tab.ldap') },
    { id: 'oidc', label: $t('settings.authentication.tab.oidc') },
    { id: 'pki', label: $t('settings.authentication.tab.pki') },
    { id: 'proxy', label: $t('settings.authentication.tab.proxy') },
    { id: 'saml', label: $t('settings.authentication.tab.saml') },
    // Directory group → in-app group/role. `/admin/group-mappings` had five
    // working endpoints and no consumer, so the feature existed only as an API.
    { id: 'mappings', label: $t('settings.authentication.tab.mappings') },
    { id: 'scim', label: $t('settings.authentication.tab.scim') },
    // Periodic LDAP reconciliation/deprovisioning sweep (issue #484). Same gap
    // as group-mappings above had: `/admin/directory-sync` was a fully working
    // API with no admin UI, so directory_sync.enabled could only ever be
    // flipped by an operator editing Postgres directly.
    { id: 'directorySync', label: $t('settings.authentication.tab.directorySync') },
    { id: 'session', label: $t('settings.authentication.tab.session') },
    // Which mailbox sends password resets, invitations and verification links.
    // It used to live only inside the Watch Sources panel, which is hidden when
    // the `watch_sources` capability is off — leaving the setting unreachable on
    // those editions even though its endpoint stayed live. It belongs here.
    { id: 'mail', label: $t('settings.authentication.tab.mail') },
    // Reads auth_config_audit (Postgres) — a different source from the "Audit
    // Log" section, which streams security events out of OpenSearch and carries
    // no configuration changes.
    { id: 'audit', label: $t('settings.authentication.tab.audit') }
  ];

  /**
   * Email configurations for the auth-mail tab. Both this list and the
   * designation endpoint are super_admin, the same tier as this whole panel,
   * so no extra gating is needed here. A failed fetch yields an empty list —
   * the designation panel then offers only "not designated", which is the
   * honest state when no configuration can be read.
   */
  let emailConfigs: EmailConfig[] = [];
  let emailConfigsLoaded = false;

  async function loadEmailConfigs() {
    emailConfigs = await getEmailConfigs().catch(() => []);
    emailConfigsLoaded = true;
  }

  onMount(async () => {
    await loadConfigs();
  });

  // Fetched lazily: most visits to this panel never open the mail tab.
  $: if (activeTab === 'mail' && !emailConfigsLoaded) loadEmailConfigs();

  // Transform array of config objects to key-value dictionary
  function transformConfigArray(configArray: any[]): Record<string, any> {
    if (!Array.isArray(configArray)) return configArray || {};
    const result: Record<string, any> = {};
    for (const item of configArray) {
      if (!item.config_key) continue;

      // A sensitive key always arrives with config_value === null — the API never
      // sends a secret or a placeholder standing in for one. `is_set` is the only
      // signal that a value exists, so it has to survive the flattening or the
      // panels have to infer it from the null, which is weaker.
      result[`${item.config_key}_is_set`] = item.is_set === true;

      if (item.config_value !== undefined && item.config_value !== null) {
        // Convert string values to appropriate types
        let value = item.config_value;
        if (item.data_type === 'bool') {
          value = value === 'true' || value === true;
        } else if (item.data_type === 'int') {
          // `|| 0` would coerce a legitimate stored 0 — parse, then fall back.
          const parsed = parseInt(value, 10);
          value = Number.isNaN(parsed) ? 0 : parsed;
        }
        result[item.config_key] = value;
      } else if (item.config_value === null) {
        // Preserve the null so a panel can distinguish "secret stored, withheld"
        // from "key absent entirely".
        result[item.config_key] = null;
      }
    }
    return result;
  }

  function applyConfigs(allConfigs: Record<string, unknown>) {
    // Transform each category's array to key-value dictionary
    const next: Record<string, any> = {};
    for (const [category, configArray] of Object.entries(allConfigs)) {
      next[category] = transformConfigArray(configArray as any[]);
    }
    configs = next;
  }

  /**
   * Read the configuration. Stale-while-revalidate: a read from a minute ago paints the forms at
   * once and the refresh only replaces it if the server's answer differs. After a save the cache
   * is dropped first, so the form always shows what the server now holds.
   */
  async function loadConfigs(afterSave = false) {
    if (afterSave) invalidateSectionData(SectionDataKey.authConfig);
    const cached = afterSave
      ? undefined
      : peekSectionData<Record<string, unknown>>(SectionDataKey.authConfig);
    if (cached) {
      applyConfigs(cached);
      configsReady = true;
    }
    try {
      const allConfigs = await loadSectionData(SectionDataKey.authConfig, () =>
        AuthConfigApi.getAllConfigs()
      );
      if (!cached || !sameSectionData(allConfigs, cached)) applyConfigs(allConfigs);
    } catch (error) {
      console.error('Failed to load auth config:', error);
      if (!cached) toastStore.error($t('settings.authentication.loadError'));
    } finally {
      configsReady = true;
    }
  }

  async function handleSave(category: string, config: Record<string, any>) {
    try {
      await AuthConfigApi.updateCategory(category, config);
      toastStore.success($t('settings.authentication.configSaved', { category }));
      hasUnsavedChanges = false;
      await loadConfigs(true);
    } catch (error) {
      console.error(`Failed to save ${category} config:`, error);
      toastStore.error($t('settings.authentication.configSaveFailed', { category }));
    }
  }

  /**
   * Save the Local tab by fanning the form out to its four owning categories.
   *
   * Each category is an independent request, so a partial failure is real and is
   * reported as such — claiming blanket success would leave the admin believing
   * a setting landed when the server rejected it.
   */
  async function handleLocalSave(config: Record<string, any>) {
    const targets = Object.entries(LOCAL_TAB_CATEGORY_KEYS)
      .map(([category, keys]) => {
        const payload: Record<string, any> = {};
        for (const key of keys) {
          if (config[key] !== undefined) payload[key] = config[key];
        }
        return { category, payload };
      })
      .filter((target) => Object.keys(target.payload).length > 0);

    const results = await Promise.allSettled(
      targets.map((target) => AuthConfigApi.updateCategory(target.category, target.payload))
    );

    const failed: string[] = [];
    results.forEach((result, index) => {
      if (result.status === 'rejected') {
        console.error(`Failed to save ${targets[index].category} config:`, result.reason);
        failed.push(targets[index].category);
      }
    });

    if (failed.length === 0) {
      toastStore.success($t('settings.authentication.localConfigSaved'));
      hasUnsavedChanges = false;
    } else {
      // Leave hasUnsavedChanges set: something in the form did not reach the server.
      toastStore.error(
        $t('settings.authentication.configSavePartialFailure', { categories: failed.join(', ') })
      );
    }

    await loadConfigs(true);
    policyRefresh += 1;
  }

  /**
   * P1.2: what the OIDC test actually discovered — `claims_supported` and
   * whether the configured roles claim is advertised. Kept here (not inside
   * `OIDCSettings`) because the panel only learns of the result through the
   * `test` event it dispatches; a Svelte event has no return channel back to
   * its own dispatcher, so the result has to come back down as a prop.
   */
  let oidcTestResult: AuthMethodTestResponse | undefined = undefined;

  async function handleTestConnection(category: string, config: Record<string, any>) {
    try {
      const result = await AuthConfigApi.testConnection(category, config);
      if (result.success) {
        toastStore.success(result.message);
      } else {
        toastStore.error(result.message);
      }
      if (category === 'oidc') {
        oidcTestResult = result;
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
    <!-- HANDOFF #28: the guided first-run flow must be re-runnable from here
         for a super_admin who skipped it. -->
    <button class="btn btn-secondary rerun-wizard-button" on:click={() => firstRunWizardStore.requestReopen()}>
      {$t('settings.authentication.rerunWizardButton')}
    </button>
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
          <p>{$t('settings.authentication.seeDoc')} <a href="https://github.com/attevon-llc/OpenTranscribe/blob/master/docs/LDAP_AUTH.md" target="_blank" rel="noopener noreferrer">LDAP_AUTH.md</a></p>
        </div>

        <div class="config-method">
          <h4>{$t('settings.authentication.method.oidc')}</h4>
          <code>OIDC_ENABLED=true</code>
          <p>{$t('settings.authentication.seeDoc')} <a href="https://github.com/attevon-llc/OpenTranscribe/blob/master/docs/OIDC_SETUP.md" target="_blank" rel="noopener noreferrer">OIDC_SETUP.md</a></p>
        </div>

        <div class="config-method">
          <h4>{$t('settings.authentication.method.pki')}</h4>
          <code>PKI_ENABLED=true</code>
          <p>{$t('settings.authentication.seeDoc')} <a href="https://github.com/attevon-llc/OpenTranscribe/blob/master/docs/PKI_SETUP.md" target="_blank" rel="noopener noreferrer">PKI_SETUP.md</a></p>
        </div>

        <div class="config-method">
          <h4>{$t('settings.authentication.method.mfa')}</h4>
          <code>MFA_ENABLED=true</code>
          <p>{$t('settings.authentication.seeDoc')} <a href="https://github.com/attevon-llc/OpenTranscribe/blob/master/.env.example" target="_blank" rel="noopener noreferrer">.env.example</a></p>
        </div>
      </div>

      <p class="note">
        <strong>{$t('settings.authentication.noteLabel')}</strong> {$t('settings.authentication.comingSoonNote')}
      </p>
    </div>
  {:else}
    <Tabs
      sticky
      wrap
      tabs={tabs}
      bind:activeId={activeTab}
      ariaLabel={$t('settings.authentication.title')}
    />

    <div class="tab-content">
      <!-- The panel chunk and the configuration load in parallel; a panel that takes config
           waits for it behind the skeleton, a self-contained one renders as soon as it lands. -->
      {#if activeTab === 'local'}
        <LazySection load={authTabLoaders.local} let:Component>
          {#if configsReady}
            <Component
              config={localTabConfig}
              refreshKey={policyRefresh}
              on:save={(e: CustomEvent<any>) => handleLocalSave(e.detail)}
              on:change={handleChange}
            />
          {:else}
            <SettingsSkeleton variant="form" count={5} label={$t('settings.authentication.loadingConfig')} />
          {/if}
        </LazySection>
      {:else if activeTab === 'ldap'}
        <LazySection load={authTabLoaders.ldap} let:Component>
          {#if configsReady}
            <Component
              config={configs.ldap || {}}
              on:save={(e: CustomEvent<any>) => handleSave('ldap', e.detail)}
              on:test={(e: CustomEvent<any>) => handleTestConnection('ldap', e.detail)}
              on:change={handleChange}
            />
          {:else}
            <SettingsSkeleton variant="form" count={5} label={$t('settings.authentication.loadingConfig')} />
          {/if}
        </LazySection>
      {:else if activeTab === 'oidc'}
        <LazySection load={authTabLoaders.oidc} let:Component>
          {#if configsReady}
            <Component
              config={configs.oidc || {}}
              secretIsSet={configs.oidc?.oidc_client_secret_is_set}
              testResult={oidcTestResult}
              on:save={(e: CustomEvent<any>) => handleSave('oidc', e.detail)}
              on:test={(e: CustomEvent<any>) => handleTestConnection('oidc', e.detail)}
              on:change={handleChange}
            />
          {:else}
            <SettingsSkeleton variant="form" count={5} label={$t('settings.authentication.loadingConfig')} />
          {/if}
        </LazySection>
      {:else if activeTab === 'pki'}
        <LazySection load={authTabLoaders.pki} let:Component>
          {#if configsReady}
            <Component
              config={configs.pki || {}}
              on:save={(e: CustomEvent<any>) => handleSave('pki', e.detail)}
              on:change={handleChange}
            />
          {:else}
            <SettingsSkeleton variant="form" count={4} label={$t('settings.authentication.loadingConfig')} />
          {/if}
        </LazySection>
      {:else if activeTab === 'proxy'}
        <LazySection load={authTabLoaders.proxy} let:Component>
          {#if configsReady}
            <Component
              config={configs.proxy || {}}
              sharedSecretIsSet={configs.proxy?.proxy_shared_secret_is_set}
              on:save={(e: CustomEvent<any>) => handleSave('proxy', e.detail)}
              on:change={handleChange}
            />
          {:else}
            <SettingsSkeleton variant="form" count={4} label={$t('settings.authentication.loadingConfig')} />
          {/if}
        </LazySection>
      {:else if activeTab === 'saml'}
        <LazySection load={authTabLoaders.saml} let:Component>
          {#if configsReady}
            <Component
              config={configs.saml || {}}
              privateKeyIsSet={configs.saml?.saml_sp_private_key_is_set}
              on:save={(e: CustomEvent<any>) => handleSave('saml', e.detail)}
              on:change={handleChange}
            />
          {:else}
            <SettingsSkeleton variant="form" count={5} label={$t('settings.authentication.loadingConfig')} />
          {/if}
        </LazySection>
      {:else if activeTab === 'mappings'}
        <!-- Self-contained: it owns its own load/save against
             /admin/group-mappings and does not share the auth-config payload. -->
        <LazySection load={authTabLoaders.mappings} let:Component><Component /></LazySection>
      {:else if activeTab === 'scim'}
        <!-- Self-contained CRUD over /admin/scim-tokens; not part of the
             auth-config payload. -->
        <LazySection load={authTabLoaders.scim} let:Component><Component /></LazySection>
      {:else if activeTab === 'directorySync'}
        <!-- Self-contained: it owns its own load/save against
             /admin/directory-sync and does not share the auth-config payload. -->
        <LazySection load={authTabLoaders.directorySync} let:Component><Component /></LazySection>
      {:else if activeTab === 'session'}
        <LazySection load={authTabLoaders.session} let:Component>
          {#if configsReady}
            <Component
              config={configs.session || {}}
              on:save={(e: CustomEvent<any>) => handleSave('session', e.detail)}
              on:change={handleChange}
            />
          {:else}
            <SettingsSkeleton variant="form" count={3} label={$t('settings.authentication.loadingConfig')} />
          {/if}
        </LazySection>
      {:else if activeTab === 'mail'}
        <LazySection load={authTabLoaders.mail} let:Component>
          {#if emailConfigsLoaded}
            <Component configs={emailConfigs} />
          {:else}
            <SettingsSkeleton variant="form" count={2} label={$t('settings.authentication.loadingConfig')} />
          {/if}
        </LazySection>
      {:else if activeTab === 'audit'}
        <LazySection load={authTabLoaders.audit} let:Component><Component /></LazySection>
      {/if}
    </div>
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

  .rerun-wizard-button {
    margin-left: auto;
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
    .config-methods {
      grid-template-columns: 1fr;
    }

  }
</style>
