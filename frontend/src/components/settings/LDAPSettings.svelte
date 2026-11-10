<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
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

  function handleTest() {
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
      <FormField size="lg" id="ldap_server" label={$t('settings.ldap.server')} hint={$t('settings.ldap.serverHelp')} let:describedby>
        <input dir="ltr"
          id="ldap_server" aria-describedby={describedby}
          type="text"
          bind:value={formData.ldap_server}
          on:input={handleChange}
          placeholder="ldap.example.com"
          disabled={!formData.ldap_enabled}
        />
      </FormField>

      <FormField size="lg" class="field-small" id="ldap_port" label={$t('settings.ldap.port')} let:describedby>
        <input
          id="ldap_port"
          aria-describedby={describedby}
          type="number"
          bind:value={formData.ldap_port}
          on:input={handleChange}
          disabled={!formData.ldap_enabled}
        />
      </FormField>
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

    <FormField size="lg" id="ldap_ca_cert_file" label={$t('settings.ldap.caCertFile')} hint={$t('settings.ldap.caCertFileHelp')} let:describedby>
      <input dir="ltr"
        id="ldap_ca_cert_file" aria-describedby={describedby}
        type="text"
        bind:value={formData.ldap_ca_cert_file}
        on:input={handleChange}
        placeholder="/etc/ssl/certs/directory-ca.pem"
        disabled={!formData.ldap_enabled || formData.ldap_tls_skip_verify === true}
      />
    </FormField>

    <FormField size="lg" id="ldap_tls_server_name" label={$t('settings.ldap.tlsServerName')} hint={$t('settings.ldap.tlsServerNameHelp')} let:describedby>
      <input dir="ltr"
        id="ldap_tls_server_name" aria-describedby={describedby}
        type="text"
        bind:value={formData.ldap_tls_server_name}
        on:input={handleChange}
        placeholder="ldap.example.com"
        disabled={!formData.ldap_enabled || formData.ldap_tls_skip_verify === true}
      />
    </FormField>

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

    <FormField size="lg" id="ldap_timeout" label={$t('settings.ldap.connectionTimeout')}>
      <input
        id="ldap_timeout"
        type="number"
        bind:value={formData.ldap_timeout}
        on:input={handleChange}
        min="1"
        max="60"
        disabled={!formData.ldap_enabled}
      />
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.ldap_enabled}>
    <h3>{$t('settings.ldap.bindCredentials')}</h3>

    <FormField size="lg" id="ldap_bind_dn" label={$t('settings.ldap.bindDn')} hint={$t('settings.ldap.bindDnHelp')} let:describedby>
      <input dir="ltr"
        id="ldap_bind_dn" aria-describedby={describedby}
        type="text"
        bind:value={formData.ldap_bind_dn}
        on:input={handleChange}
        placeholder="cn=admin,dc=example,dc=com"
        disabled={!formData.ldap_enabled}
      />
    </FormField>

    <FormField size="lg" id="ldap_bind_password" label={$t('settings.ldap.bindPassword')} hint={$t('settings.ldap.bindPasswordHelp')} let:describedby>
      <input dir="ltr"
        id="ldap_bind_password" aria-describedby={describedby}
        type="password"
        bind:value={formData.ldap_bind_password}
        on:input={handleChange}
        placeholder={$t('settings.ldap.enterPassword')}
        disabled={!formData.ldap_enabled}
      />
    </FormField>

    <FormField size="lg" id="ldap_search_base" label={$t('settings.ldap.searchBase')} hint={$t('settings.ldap.searchBaseHelp')} let:describedby>
      <input
        id="ldap_search_base" aria-describedby={describedby}
        type="text"
        bind:value={formData.ldap_search_base}
        on:input={handleChange}
        placeholder="dc=example,dc=com"
        disabled={!formData.ldap_enabled}
      />
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.ldap_enabled}>
    <h3>{$t('settings.ldap.userAttributes')}</h3>

    <div class="form-row thirds">
      <FormField size="lg" id="ldap_username_attr" label={$t('settings.ldap.usernameAttr')}>
        <input dir="ltr"
          id="ldap_username_attr"
          type="text"
          bind:value={formData.ldap_username_attr}
          on:input={handleChange}
          placeholder="sAMAccountName"
          disabled={!formData.ldap_enabled}
        />
      </FormField>

      <FormField size="lg" id="ldap_email_attr" label={$t('settings.ldap.emailAttr')}>
        <input dir="ltr"
          id="ldap_email_attr"
          type="text"
          bind:value={formData.ldap_email_attr}
          on:input={handleChange}
          placeholder="mail"
          disabled={!formData.ldap_enabled}
        />
      </FormField>

      <FormField size="lg" id="ldap_name_attr" label={$t('settings.ldap.displayNameAttr')}>
        <input dir="ltr"
          id="ldap_name_attr"
          type="text"
          bind:value={formData.ldap_name_attr}
          on:input={handleChange}
          placeholder="displayName"
          disabled={!formData.ldap_enabled}
        />
      </FormField>
    </div>

    <FormField size="lg" id="ldap_user_search_filter" label={$t('settings.ldap.userSearchFilter')} hint={$t('settings.ldap.userSearchFilterHelp')} let:describedby>
      <input dir="ltr"
        id="ldap_user_search_filter" aria-describedby={describedby}
        type="text"
        bind:value={formData.ldap_user_search_filter}
        on:input={handleChange}
        placeholder="(uid={'{username}'})"
        disabled={!formData.ldap_enabled}
      />
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.ldap_enabled}>
    <h3>{$t('settings.ldap.groupSettings')}</h3>

    <FormField size="lg" id="ldap_group_attr" label={$t('settings.ldap.groupMembershipAttr')}>
      <input dir="ltr"
        id="ldap_group_attr"
        type="text"
        bind:value={formData.ldap_group_attr}
        on:input={handleChange}
        placeholder="memberOf"
        disabled={!formData.ldap_enabled}
      />
    </FormField>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.ldap_recursive_groups}
        on:change={handleChange}
        disabled={!formData.ldap_enabled}
      />
      <span>{$t('settings.ldap.recursiveGroups')}</span>
    </label>

    <FormField size="lg" id="ldap_admin_groups" label={$t('settings.ldap.adminGroups')} hint={$t('settings.ldap.adminGroupsHelp')} let:describedby>
      <input dir="ltr"
        id="ldap_admin_groups" aria-describedby={describedby}
        type="text"
        bind:value={formData.ldap_admin_groups}
        on:input={handleChange}
        placeholder="CN=Admins,OU=Groups,DC=example,DC=com"
        disabled={!formData.ldap_enabled}
      />
    </FormField>

    <FormField size="lg" id="ldap_admin_users" label={$t('settings.ldap.adminUsers')} hint={$t('settings.ldap.adminUsersHelp')} let:describedby>
      <input dir="ltr"
        id="ldap_admin_users" aria-describedby={describedby}
        type="text"
        bind:value={formData.ldap_admin_users}
        on:input={handleChange}
        placeholder="admin,superuser"
        disabled={!formData.ldap_enabled}
      />
    </FormField>

    <FormField size="lg" id="ldap_user_groups" label={$t('settings.ldap.allowedUserGroups')} hint={$t('settings.ldap.allowedUserGroupsHelp')} let:describedby>
      <input dir="ltr"
        id="ldap_user_groups" aria-describedby={describedby}
        type="text"
        bind:value={formData.ldap_user_groups}
        on:input={handleChange}
        placeholder="CN=Users,OU=Groups,DC=example,DC=com"
        disabled={!formData.ldap_enabled}
      />
    </FormField>
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
    margin-bottom: var(--space-6);
    padding-bottom: var(--space-4);
    border-bottom: 1px solid var(--color-border);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    cursor: pointer;
  }

  .toggle-label input[type="checkbox"] {
    width: 1.25rem;
    height: 1.25rem;
    cursor: pointer;
  }

  .toggle-text {
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-base);
  }

  .section {
    margin-bottom: var(--space-8);
    padding: var(--space-4);
    background: var(--color-bg-secondary);
    border-radius: var(--radius-lg);
    transition: opacity var(--duration-base);
  }

  .section.disabled {
    opacity: 0.5;
    pointer-events: none;
  }

  .section h3 {
    margin: 0 0 var(--space-4) 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--color-text-secondary);
  }

  .form-row {
    display: flex;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
  }

  .form-row.checkboxes {
    gap: var(--space-8);
  }

  .section :global(.form-field.field-small) {
    flex: 0 0 100px;
  }

  .section :global(.form-field) {
    flex: 1;
    margin-bottom: var(--space-4);
  }


  .section :global(.form-field input[type="text"]),
  .section :global(.form-field input[type="password"]),
  .section :global(.form-field input[type="number"]) {
    width: 100%;
  }

  .section :global(.form-field input:disabled) {
    cursor: not-allowed;
  }

  .help-text {
    display: block;
    margin-top: var(--space-1);
    font-size: var(--font-size-xs);
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
    gap: var(--space-2);
    cursor: pointer;
    font-size: var(--font-size-md);
    margin-bottom: var(--space-3);
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
    gap: var(--space-4);
    justify-content: flex-end;
    padding-top: var(--space-4);
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
      gap: var(--space-3);
    }

    .section :global(.form-field.field-small) {
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
