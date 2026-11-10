<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { LDAPConfig } from '$lib/api/authConfig';

  export let config: Partial<LDAPConfig> = {};

  const dispatch = createEventDispatcher();

  const LDAP_DEFAULTS: LDAPConfig = {
    ldap_enabled: false,
    ldap_server: '',
    ldap_port: 389,
    ldap_use_ssl: false,
    ldap_use_tls: true,
    ldap_ca_cert_file: '',
    ldap_tls_server_name: '',
    ldap_tls_skip_verify: null,
    ldap_bind_dn: '',
    ldap_bind_password: '',
    ldap_search_base: '',
    ldap_username_attr: 'sAMAccountName',
    ldap_email_attr: 'mail',
    ldap_name_attr: 'displayName',
    ldap_user_search_filter: '({username_attr}={username})',
    ldap_timeout: 10,
    ldap_admin_users: '',
    ldap_admin_groups: '',
    ldap_user_groups: '',
    ldap_recursive_groups: true,
    ldap_group_attr: 'memberOf'
  };

  // A null or missing field falls back to its default; unknown keys are dropped.
  function withDefaults(config: Partial<LDAPConfig>): LDAPConfig {
    const merged: LDAPConfig = { ...LDAP_DEFAULTS };
    for (const key of Object.keys(LDAP_DEFAULTS) as (keyof LDAPConfig)[]) {
      Object.assign(merged, { [key]: config[key] ?? LDAP_DEFAULTS[key] });
    }
    return merged;
  }

  let formData: LDAPConfig = withDefaults(config);

  let testing = false;
  let saving = false;

  $: if (config) {
    formData = withDefaults(config);
  }

  function handleChange() {
    dispatch('change');
  }

  // Not bind:checked: Svelte writes the unchecked DOM state back into a null bound value at
  // mount, which would turn "not set here" into an explicit false on the next save.
  function handleSkipVerifyChange(event: Event) {
    formData.ldap_tls_skip_verify = (event.currentTarget as HTMLInputElement).checked;
    handleChange();
  }

  function handleSave() {
    saving = true;
    dispatch('save', formData);
    setTimeout(() => saving = false, 500);
  }

  async function handleTest() {
    testing = true;
    dispatch('test', formData);
    setTimeout(() => testing = false, 2000);
  }

  function handlePortChange() {
    // Auto-update port based on SSL setting
    if (formData.ldap_use_ssl && formData.ldap_port === 389) {
      formData.ldap_port = 636;
    } else if (!formData.ldap_use_ssl && formData.ldap_port === 636) {
      formData.ldap_port = 389;
    }
    handleChange();
  }
</script>

<div class="settings-panel">
  <div class="enable-toggle">
    <label class="toggle-label">
      <input
        type="checkbox"
        bind:checked={formData.ldap_enabled}
        on:change={handleChange}
      />
      <span class="toggle-text">{$t('settings.ldap.enable')}</span>
    </label>
  </div>

  <div class="section" class:disabled={!formData.ldap_enabled}>
    <h3>{$t('settings.ldap.serverConnection')}</h3>

    <div class="form-row">
      <div class="form-group">
        <label for="ldap_server">{$t('settings.ldap.server')}</label>
        <input dir="ltr"
          id="ldap_server"
          type="text"
          bind:value={formData.ldap_server}
          on:input={handleChange}
          placeholder="ldap.example.com"
          disabled={!formData.ldap_enabled}
        />
        <span class="help-text">{$t('settings.ldap.serverHelp')}</span>
      </div>

      <div class="form-group small">
        <label for="ldap_port">{$t('settings.ldap.port')}</label>
        <input
          id="ldap_port"
          type="number"
          bind:value={formData.ldap_port}
          on:input={handleChange}
          disabled={!formData.ldap_enabled}
        />
      </div>
    </div>

    <div class="form-row checkboxes">
      <label class="checkbox-label">
        <input
          type="checkbox"
          bind:checked={formData.ldap_use_ssl}
          on:change={handlePortChange}
          disabled={!formData.ldap_enabled}
        />
        <span>{$t('settings.ldap.useSsl')}</span>
      </label>

      <label class="checkbox-label">
        <input
          type="checkbox"
          bind:checked={formData.ldap_use_tls}
          on:change={handleChange}
          disabled={!formData.ldap_enabled || formData.ldap_use_ssl}
        />
        <span>{$t('settings.ldap.useStartTls')}</span>
      </label>
    </div>

    <p class="help-text">{$t('settings.ldap.tlsEnvFallbackHelp')}</p>

    <div class="form-group">
      <label for="ldap_ca_cert_file">{$t('settings.ldap.caCertFile')}</label>
      <input dir="ltr"
        id="ldap_ca_cert_file"
        type="text"
        bind:value={formData.ldap_ca_cert_file}
        on:input={handleChange}
        placeholder="/etc/ssl/certs/directory-ca.pem"
        disabled={!formData.ldap_enabled || formData.ldap_tls_skip_verify === true}
      />
      <span class="help-text">{$t('settings.ldap.caCertFileHelp')}</span>
    </div>

    <div class="form-group">
      <label for="ldap_tls_server_name">{$t('settings.ldap.tlsServerName')}</label>
      <input dir="ltr"
        id="ldap_tls_server_name"
        type="text"
        bind:value={formData.ldap_tls_server_name}
        on:input={handleChange}
        placeholder="ldap.example.com"
        disabled={!formData.ldap_enabled || formData.ldap_tls_skip_verify === true}
      />
      <span class="help-text">{$t('settings.ldap.tlsServerNameHelp')}</span>
    </div>

    <label class="checkbox-label">
      <input
        type="checkbox"
        checked={formData.ldap_tls_skip_verify === true}
        on:change={handleSkipVerifyChange}
        disabled={!formData.ldap_enabled}
      />
      <span>{$t('settings.ldap.tlsSkipVerify')}</span>
    </label>
    {#if formData.ldap_tls_skip_verify}
      <p class="insecure-warning" role="alert">{$t('settings.ldap.tlsSkipVerifyWarning')}</p>
    {/if}

    <div class="form-group">
      <label for="ldap_timeout">{$t('settings.ldap.connectionTimeout')}</label>
      <input
        id="ldap_timeout"
        type="number"
        bind:value={formData.ldap_timeout}
        on:input={handleChange}
        min="1"
        max="60"
        disabled={!formData.ldap_enabled}
      />
    </div>
  </div>

  <div class="section" class:disabled={!formData.ldap_enabled}>
    <h3>{$t('settings.ldap.bindCredentials')}</h3>

    <div class="form-group">
      <label for="ldap_bind_dn">{$t('settings.ldap.bindDn')}</label>
      <input dir="ltr"
        id="ldap_bind_dn"
        type="text"
        bind:value={formData.ldap_bind_dn}
        on:input={handleChange}
        placeholder="cn=admin,dc=example,dc=com"
        disabled={!formData.ldap_enabled}
      />
      <span class="help-text">{$t('settings.ldap.bindDnHelp')}</span>
    </div>

    <div class="form-group">
      <label for="ldap_bind_password">{$t('settings.ldap.bindPassword')}</label>
      <input dir="ltr"
        id="ldap_bind_password"
        type="password"
        bind:value={formData.ldap_bind_password}
        on:input={handleChange}
        placeholder={$t('settings.ldap.enterPassword')}
        disabled={!formData.ldap_enabled}
      />
      <span class="help-text">{$t('settings.ldap.bindPasswordHelp')}</span>
    </div>

    <div class="form-group">
      <label for="ldap_search_base">{$t('settings.ldap.searchBase')}</label>
      <input
        id="ldap_search_base"
        type="text"
        bind:value={formData.ldap_search_base}
        on:input={handleChange}
        placeholder="dc=example,dc=com"
        disabled={!formData.ldap_enabled}
      />
      <span class="help-text">{$t('settings.ldap.searchBaseHelp')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.ldap_enabled}>
    <h3>{$t('settings.ldap.userAttributes')}</h3>

    <div class="form-row thirds">
      <div class="form-group">
        <label for="ldap_username_attr">{$t('settings.ldap.usernameAttr')}</label>
        <input dir="ltr"
          id="ldap_username_attr"
          type="text"
          bind:value={formData.ldap_username_attr}
          on:input={handleChange}
          placeholder="sAMAccountName"
          disabled={!formData.ldap_enabled}
        />
      </div>

      <div class="form-group">
        <label for="ldap_email_attr">{$t('settings.ldap.emailAttr')}</label>
        <input dir="ltr"
          id="ldap_email_attr"
          type="text"
          bind:value={formData.ldap_email_attr}
          on:input={handleChange}
          placeholder="mail"
          disabled={!formData.ldap_enabled}
        />
      </div>

      <div class="form-group">
        <label for="ldap_name_attr">{$t('settings.ldap.displayNameAttr')}</label>
        <input dir="ltr"
          id="ldap_name_attr"
          type="text"
          bind:value={formData.ldap_name_attr}
          on:input={handleChange}
          placeholder="displayName"
          disabled={!formData.ldap_enabled}
        />
      </div>
    </div>

    <div class="form-group">
      <label for="ldap_user_search_filter">{$t('settings.ldap.userSearchFilter')}</label>
      <input dir="ltr"
        id="ldap_user_search_filter"
        type="text"
        bind:value={formData.ldap_user_search_filter}
        on:input={handleChange}
        placeholder="(uid={'{username}'})"
        disabled={!formData.ldap_enabled}
      />
      <span class="help-text">{$t('settings.ldap.userSearchFilterHelp')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.ldap_enabled}>
    <h3>{$t('settings.ldap.groupSettings')}</h3>

    <div class="form-group">
      <label for="ldap_group_attr">{$t('settings.ldap.groupMembershipAttr')}</label>
      <input dir="ltr"
        id="ldap_group_attr"
        type="text"
        bind:value={formData.ldap_group_attr}
        on:input={handleChange}
        placeholder="memberOf"
        disabled={!formData.ldap_enabled}
      />
    </div>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.ldap_recursive_groups}
        on:change={handleChange}
        disabled={!formData.ldap_enabled}
      />
      <span>{$t('settings.ldap.recursiveGroups')}</span>
    </label>

    <div class="form-group">
      <label for="ldap_admin_groups">{$t('settings.ldap.adminGroups')}</label>
      <input dir="ltr"
        id="ldap_admin_groups"
        type="text"
        bind:value={formData.ldap_admin_groups}
        on:input={handleChange}
        placeholder="CN=Admins,OU=Groups,DC=example,DC=com"
        disabled={!formData.ldap_enabled}
      />
      <span class="help-text">{$t('settings.ldap.adminGroupsHelp')}</span>
    </div>

    <div class="form-group">
      <label for="ldap_admin_users">{$t('settings.ldap.adminUsers')}</label>
      <input dir="ltr"
        id="ldap_admin_users"
        type="text"
        bind:value={formData.ldap_admin_users}
        on:input={handleChange}
        placeholder="admin,superuser"
        disabled={!formData.ldap_enabled}
      />
      <span class="help-text">{$t('settings.ldap.adminUsersHelp')}</span>
    </div>

    <div class="form-group">
      <label for="ldap_user_groups">{$t('settings.ldap.allowedUserGroups')}</label>
      <input dir="ltr"
        id="ldap_user_groups"
        type="text"
        bind:value={formData.ldap_user_groups}
        on:input={handleChange}
        placeholder="CN=Users,OU=Groups,DC=example,DC=com"
        disabled={!formData.ldap_enabled}
      />
      <span class="help-text">{$t('settings.ldap.allowedUserGroupsHelp')}</span>
    </div>
  </div>

  <div class="actions">
    <button
      class="btn btn-secondary"
      on:click={handleTest}
      disabled={!formData.ldap_enabled || testing}
    >
      {testing ? $t('common.testing') : $t('settings.ldap.testConnection')}
    </button>
    <button
      class="btn btn-primary"
      on:click={handleSave}
      disabled={saving}
    >
      {saving ? $t('common.saving') : $t('settings.ldap.saveConfiguration')}
    </button>
  </div>
</div>

<style>
  .settings-panel {
    max-width: 800px;
  }

  .enable-toggle {
    margin-bottom: 1.5rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid var(--color-border);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    cursor: pointer;
  }

  .toggle-label input[type="checkbox"] {
    width: 1.25rem;
    height: 1.25rem;
    cursor: pointer;
  }

  .toggle-text {
    font-weight: 500;
    font-size: 1rem;
  }

  .section {
    margin-bottom: 2rem;
    padding: 1rem;
    background: var(--color-bg-secondary);
    border-radius: 8px;
    transition: opacity var(--duration-base);
  }

  .section.disabled {
    opacity: 0.5;
    pointer-events: none;
  }

  .section h3 {
    margin: 0 0 1rem 0;
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--color-text-secondary);
  }

  .form-row {
    display: flex;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .form-row.checkboxes {
    gap: 2rem;
  }

  .form-row.thirds .form-group {
    flex: 1;
  }

  .form-group {
    flex: 1;
    margin-bottom: 1rem;
  }

  .form-group.small {
    flex: 0 0 100px;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.5rem;
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--color-text);
  }

  .form-group input[type="text"],
  .form-group input[type="password"],
  .form-group input[type="number"] {
    width: 100%;
  }

  .form-group input:disabled {
    cursor: not-allowed;
  }

  .help-text {
    display: block;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
  }

  .insecure-warning {
    margin: 0 0 var(--space-3);
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-sm);
    border: 1px solid var(--color-warning-border);
    border-radius: var(--radius-md);
    background: var(--color-warning-bg);
    color: var(--color-warning-text);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-size: 0.875rem;
    margin-bottom: 0.75rem;
  }

  .checkbox-label input[type="checkbox"] {
    width: 1rem;
    height: 1rem;
    cursor: pointer;
  }

  .checkbox-label input:disabled {
    cursor: not-allowed;
  }

  .actions {
    display: flex;
    gap: 1rem;
    justify-content: flex-end;
    padding-top: 1rem;
    border-top: 1px solid var(--color-border);
  }

  .actions .btn-secondary {
    margin-right: auto;
  }

  @media (max-width: 768px) {
    .form-row {
      flex-direction: column;
      gap: 0;
    }

    .form-row.checkboxes {
      gap: 0.75rem;
    }

    .form-group.small {
      flex: 1;
    }

    .actions {
      flex-wrap: wrap;
    }

    .actions .btn-secondary {
      margin-right: 0;
    }

    .actions .btn {
      flex: 1;
      min-height: 44px;
    }
  }
</style>
