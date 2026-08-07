<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { KeycloakConfig } from '$lib/api/authConfig';

  export let config: Partial<KeycloakConfig> = {};

  /**
   * Whether the backend reports a stored client secret (`is_set` on the sensitive
   * config row). The secret itself never reaches the browser, so this is the only
   * signal available for the "leave blank to keep the current value" affordance.
   *
   * Left `undefined` we fall back to `config.keycloak_client_secret === null`,
   * which is what a stored sensitive row flattens to (the key is absent entirely
   * when no row exists).
   */
  export let secretIsSet: boolean | undefined = undefined;

  const dispatch = createEventDispatcher();

  /**
   * One builder for both the initial value and the reactive rebuild. These were
   * two hand-maintained copies; a field added to only one of them silently reset
   * itself the first time the parent reloaded the config.
   *
   * `keycloak_client_secret` is deliberately absent — see `clientSecretInput`.
   */
  function buildFormData(source: Partial<KeycloakConfig>): KeycloakConfig {
    return {
      keycloak_enabled: source.keycloak_enabled ?? false,
      keycloak_server_url: source.keycloak_server_url ?? '',
      keycloak_internal_url: source.keycloak_internal_url ?? '',
      keycloak_discovery_url: source.keycloak_discovery_url ?? '',
      keycloak_realm: source.keycloak_realm ?? '',
      keycloak_client_id: source.keycloak_client_id ?? '',
      keycloak_callback_url: source.keycloak_callback_url ?? '',
      keycloak_admin_role: source.keycloak_admin_role ?? 'admin',
      keycloak_roles_claim: source.keycloak_roles_claim ?? 'realm_access.roles',
      keycloak_issuer: source.keycloak_issuer ?? '',
      keycloak_scopes: source.keycloak_scopes ?? 'openid email profile',
      keycloak_timeout: source.keycloak_timeout ?? 30,
      keycloak_verify_audience: source.keycloak_verify_audience ?? true,
      keycloak_audience: source.keycloak_audience ?? '',
      keycloak_use_pkce: source.keycloak_use_pkce ?? true,
      keycloak_verify_issuer: source.keycloak_verify_issuer ?? true
    };
  }

  let formData: KeycloakConfig = buildFormData(config);

  /**
   * Held outside `formData` so a secret is only ever submitted when the admin
   * typed one. The field used to bind whatever the API returned — which was the
   * literal `***REDACTED***` — and Save then wrote that placeholder over the real
   * client secret.
   */
  let clientSecretInput = '';

  let testing = false;
  let saving = false;
  let showSecret = false;

  $: if (config) {
    formData = buildFormData(config);
    // A reload means the previous edit either landed or was discarded; either way
    // the typed secret must not survive into the next save.
    clientSecretInput = '';
  }

  $: clientSecretIsSet = secretIsSet ?? config.keycloak_client_secret === null;

  /** A discovery URL supersedes the realm-based endpoint construction. */
  $: discoveryActive = (formData.keycloak_discovery_url ?? '').trim() !== '';

  /** Payload for save/test: everything in the form, plus the secret only if typed. */
  function buildPayload(): KeycloakConfig {
    return clientSecretInput === ''
      ? { ...formData }
      : { ...formData, keycloak_client_secret: clientSecretInput };
  }

  function handleChange() {
    dispatch('change');
  }

  function handleSave() {
    saving = true;
    dispatch('save', buildPayload());
    setTimeout(() => saving = false, 500);
  }

  async function handleTest() {
    testing = true;
    dispatch('test', buildPayload());
    setTimeout(() => testing = false, 2000);
  }

  function generateCallbackUrl() {
    if (typeof window !== 'undefined') {
      formData.keycloak_callback_url = `${window.location.origin}/api/auth/keycloak/callback`;
      handleChange();
    }
  }
</script>

<div class="settings-panel">
  <div class="enable-toggle">
    <label class="toggle-label">
      <input
        type="checkbox"
        bind:checked={formData.keycloak_enabled}
        on:change={handleChange}
      />
      <span class="toggle-text">{$t('settings.keycloak.enable')}</span>
    </label>
  </div>

  <div class="info-box">
    <strong>{$t('settings.keycloak.oidcTitle')}</strong>
    <p>{$t('settings.keycloak.oidcDescription')}</p>
  </div>

  <div class="section" class:disabled={!formData.keycloak_enabled}>
    <h3>{$t('settings.keycloak.serverConfiguration')}</h3>

    <div class="form-group">
      <label for="keycloak_server_url">{$t('settings.keycloak.serverUrlPublic')}</label>
      <input
        id="keycloak_server_url"
        type="text"
        bind:value={formData.keycloak_server_url}
        on:input={handleChange}
        placeholder="https://keycloak.example.com"
        disabled={!formData.keycloak_enabled}
      />
      <span class="help-text">{$t('settings.keycloak.serverUrlPublicHelp')}</span>
    </div>

    <div class="form-group">
      <label for="keycloak_internal_url">{$t('settings.keycloak.serverUrlInternal')}</label>
      <input
        id="keycloak_internal_url"
        type="text"
        bind:value={formData.keycloak_internal_url}
        on:input={handleChange}
        placeholder="http://keycloak:8080"
        disabled={!formData.keycloak_enabled}
      />
      <span class="help-text">{$t('settings.keycloak.serverUrlInternalHelp')}</span>
    </div>

    <div class="form-group">
      <label for="keycloak_discovery_url">{$t('settings.keycloak.discoveryUrl')}</label>
      <input
        id="keycloak_discovery_url"
        type="text"
        bind:value={formData.keycloak_discovery_url}
        on:input={handleChange}
        placeholder="https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration"
        disabled={!formData.keycloak_enabled}
      />
      <span class="help-text">{$t('settings.keycloak.discoveryUrlHelp')}</span>
    </div>

    <div class="form-group" class:superseded={discoveryActive}>
      <label for="keycloak_realm">{$t('settings.keycloak.realm')}</label>
      <input
        id="keycloak_realm"
        type="text"
        bind:value={formData.keycloak_realm}
        on:input={handleChange}
        placeholder="master"
        disabled={!formData.keycloak_enabled}
      />
      <span class="help-text">{$t('settings.keycloak.realmHelp')}</span>
      {#if discoveryActive}
        <span class="help-text superseded-note">{$t('settings.keycloak.realmSupersededHelp')}</span>
      {/if}
    </div>

    <div class="form-group">
      <label for="keycloak_timeout">{$t('settings.keycloak.requestTimeout')}</label>
      <input
        id="keycloak_timeout"
        type="number"
        bind:value={formData.keycloak_timeout}
        on:input={handleChange}
        min="5"
        max="120"
        disabled={!formData.keycloak_enabled}
      />
    </div>
  </div>

  <div class="section" class:disabled={!formData.keycloak_enabled}>
    <h3>{$t('settings.keycloak.clientConfiguration')}</h3>

    <div class="form-row">
      <div class="form-group">
        <label for="keycloak_client_id">{$t('settings.keycloak.clientId')}</label>
        <input
          id="keycloak_client_id"
          type="text"
          bind:value={formData.keycloak_client_id}
          on:input={handleChange}
          placeholder="opentranscribe"
          disabled={!formData.keycloak_enabled}
        />
      </div>

      <div class="form-group">
        <label for="keycloak_client_secret">{$t('settings.keycloak.clientSecret')}</label>
        <div class="input-with-toggle">
          <input
            id="keycloak_client_secret"
            type={showSecret ? 'text' : 'password'}
            bind:value={clientSecretInput}
            on:input={handleChange}
            autocomplete="new-password"
            placeholder={clientSecretIsSet
              ? $t('settings.keycloak.clientSecretKeepPlaceholder')
              : $t('settings.keycloak.enterClientSecret')}
            disabled={!formData.keycloak_enabled}
          />
          <button
            type="button"
            class="toggle-visibility"
            on:click={() => showSecret = !showSecret}
            disabled={!formData.keycloak_enabled}
          >
            {showSecret ? $t('common.hide') : $t('common.show')}
          </button>
        </div>
        <span class="help-text">
          {clientSecretIsSet
            ? $t('settings.keycloak.clientSecretSetHelp')
            : $t('settings.keycloak.clientSecretHelp')}
        </span>
      </div>
    </div>

    <div class="form-group">
      <label for="keycloak_callback_url">{$t('settings.keycloak.callbackUrl')}</label>
      <div class="input-with-button">
        <input
          id="keycloak_callback_url"
          type="text"
          bind:value={formData.keycloak_callback_url}
          on:input={handleChange}
          placeholder="https://app.example.com/api/auth/keycloak/callback"
          disabled={!formData.keycloak_enabled}
        />
        <button
          type="button"
          class="btn btn-small"
          on:click={generateCallbackUrl}
          disabled={!formData.keycloak_enabled}
        >
          {$t('settings.keycloak.autoDetect')}
        </button>
      </div>
      <span class="help-text">{$t('settings.keycloak.callbackUrlHelp')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.keycloak_enabled}>
    <h3>{$t('settings.keycloak.securityOptions')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.keycloak_use_pkce}
        on:change={handleChange}
        disabled={!formData.keycloak_enabled}
      />
      <span>{$t('settings.keycloak.usePkce')}</span>
    </label>
    <span class="help-text indented">{$t('settings.keycloak.usePkceHelp')}</span>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.keycloak_verify_issuer}
        on:change={handleChange}
        disabled={!formData.keycloak_enabled}
      />
      <span>{$t('settings.keycloak.verifyIssuer')}</span>
    </label>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.keycloak_verify_audience}
        on:change={handleChange}
        disabled={!formData.keycloak_enabled}
      />
      <span>{$t('settings.keycloak.verifyAudience')}</span>
    </label>

    {#if formData.keycloak_verify_audience}
      <div class="form-group indented">
        <label for="keycloak_audience">{$t('settings.keycloak.expectedAudience')}</label>
        <input
          id="keycloak_audience"
          type="text"
          bind:value={formData.keycloak_audience}
          on:input={handleChange}
          placeholder={$t('settings.keycloak.expectedAudiencePlaceholder')}
          disabled={!formData.keycloak_enabled}
        />
      </div>
    {/if}
  </div>

  <div class="section" class:disabled={!formData.keycloak_enabled}>
    <h3>{$t('settings.keycloak.roleMapping')}</h3>

    <div class="form-group">
      <label for="keycloak_admin_role">{$t('settings.keycloak.adminRoleName')}</label>
      <input
        id="keycloak_admin_role"
        type="text"
        bind:value={formData.keycloak_admin_role}
        on:input={handleChange}
        placeholder="admin"
        disabled={!formData.keycloak_enabled}
      />
      <span class="help-text">{$t('settings.keycloak.adminRoleHelp')}</span>
    </div>

    <div class="form-group">
      <label for="keycloak_roles_claim">{$t('settings.keycloak.rolesClaim')}</label>
      <input
        id="keycloak_roles_claim"
        type="text"
        bind:value={formData.keycloak_roles_claim}
        on:input={handleChange}
        placeholder="realm_access.roles"
        disabled={!formData.keycloak_enabled}
      />
      <span class="help-text">{$t('settings.keycloak.rolesClaimHelp')}</span>
      <span class="help-text">{$t('settings.keycloak.rolesClaimWarning')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.keycloak_enabled}>
    <h3>{$t('settings.keycloak.advancedOptions')}</h3>

    <div class="form-group">
      <label for="keycloak_scopes">{$t('settings.keycloak.scopes')}</label>
      <input
        id="keycloak_scopes"
        type="text"
        bind:value={formData.keycloak_scopes}
        on:input={handleChange}
        placeholder="openid email profile"
        disabled={!formData.keycloak_enabled}
      />
      <span class="help-text">{$t('settings.keycloak.scopesHelp')}</span>
    </div>

    <div class="form-group">
      <label for="keycloak_issuer">{$t('settings.keycloak.issuer')}</label>
      <input
        id="keycloak_issuer"
        type="text"
        bind:value={formData.keycloak_issuer}
        on:input={handleChange}
        placeholder={$t('settings.keycloak.issuerPlaceholder')}
        disabled={!formData.keycloak_enabled}
      />
      <span class="help-text">{$t('settings.keycloak.issuerHelp')}</span>
    </div>
  </div>

  <div class="actions">
    <button
      class="btn btn-secondary"
      on:click={handleTest}
      disabled={!formData.keycloak_enabled || testing}
    >
      {testing ? $t('common.testing') : $t('settings.keycloak.testConnection')}
    </button>
    <button
      class="btn btn-primary"
      on:click={handleSave}
      disabled={saving}
    >
      {saving ? $t('common.saving') : $t('settings.keycloak.saveConfiguration')}
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

  .info-box {
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1.5rem;
  }

  .info-box strong {
    color: var(--color-text);
  }

  .info-box p {
    margin: 0.5rem 0 0 0;
    font-size: 0.875rem;
    color: var(--color-text-secondary);
  }

  .section {
    margin-bottom: 2rem;
    padding: 1rem;
    background: var(--color-bg-secondary);
    border-radius: 8px;
    transition: opacity 0.2s;
  }

  .section.disabled {
    opacity: 0.5;
    pointer-events: none;
  }

  .section h3 {
    margin: 0 0 1rem 0;
    font-size: 0.875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--color-text-secondary);
  }

  .form-row {
    display: flex;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .form-group {
    flex: 1;
    margin-bottom: 1rem;
  }

  .form-group.indented {
    margin-left: 1.5rem;
  }

  /*
   * A discovery URL makes the realm irrelevant. Fade it rather than disable it —
   * an admin may be mid-edit, and a disabled field also loses its value on save.
   */
  .form-group.superseded label,
  .form-group.superseded input {
    opacity: 0.55;
  }

  .superseded-note {
    color: var(--color-warning-text);
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
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 0.875rem;
  }

  .form-group input:focus {
    outline: none;
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px var(--color-primary-alpha);
  }

  .form-group input:disabled {
    background: var(--color-bg-tertiary);
    cursor: not-allowed;
  }

  .input-with-toggle,
  .input-with-button {
    display: flex;
    gap: 0.5rem;
  }

  .input-with-toggle input,
  .input-with-button input {
    flex: 1;
  }

  .toggle-visibility {
    padding: 0.5rem 0.75rem;
    background: var(--color-bg-tertiary);
    border: 1px solid var(--color-border);
    border-radius: 4px;
    color: var(--color-text-secondary);
    font-size: 0.75rem;
    cursor: pointer;
    white-space: nowrap;
  }

  .toggle-visibility:hover:not(:disabled) {
    background: var(--color-bg-hover);
  }

  .toggle-visibility:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }

  .btn-small {
    padding: 0.5rem 0.75rem;
    font-size: 0.75rem;
    background-color: #3b82f6;
    color: white;
    border: none;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-small:hover:not(:disabled) {
    background-color: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-small:active:not(:disabled) {
    transform: scale(1);
  }

  .help-text {
    display: block;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
  }

  .help-text.indented {
    margin-left: 1.5rem;
    margin-bottom: 0.75rem;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-size: 0.875rem;
    margin-bottom: 0.5rem;
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
