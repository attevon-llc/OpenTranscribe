<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { OIDCConfig, AuthMethodTestResponse } from '$lib/api/authConfig';
  import {
    OIDC_PROVIDER_PRESETS,
    findOIDCPreset,
    oidcPresetFieldValues
  } from '$lib/utils/oidcProviderPresets';

  export let config: Partial<OIDCConfig> = {};

  /**
   * P1.2: what Test Connection actually discovered from the provider's
   * `.well-known/openid-configuration` — `claims_supported` and whether the
   * configured roles claim is advertised. The parent owns this because a
   * Svelte event has no return channel back to the dispatcher that fired it.
   */
  export let testResult: AuthMethodTestResponse | undefined = undefined;

  /**
   * Whether the backend reports a stored client secret (`is_set` on the sensitive
   * config row). The secret itself never reaches the browser, so this is the only
   * signal available for the "leave blank to keep the current value" affordance.
   *
   * Left `undefined` we fall back to `config.oidc_client_secret === null`,
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
   * `oidc_client_secret` is deliberately absent — see `clientSecretInput`.
   */
  function buildFormData(source: Partial<OIDCConfig>): OIDCConfig {
    return {
      oidc_enabled: source.oidc_enabled ?? false,
      oidc_server_url: source.oidc_server_url ?? '',
      oidc_internal_url: source.oidc_internal_url ?? '',
      oidc_discovery_url: source.oidc_discovery_url ?? '',
      oidc_realm: source.oidc_realm ?? '',
      oidc_client_id: source.oidc_client_id ?? '',
      oidc_callback_url: source.oidc_callback_url ?? '',
      oidc_admin_role: source.oidc_admin_role ?? 'admin',
      oidc_roles_claim: source.oidc_roles_claim ?? 'realm_access.roles',
      oidc_issuer: source.oidc_issuer ?? '',
      oidc_scopes: source.oidc_scopes ?? 'openid email profile',
      oidc_allowed_groups: source.oidc_allowed_groups ?? '',
      oidc_blocked_groups: source.oidc_blocked_groups ?? '',
      oidc_timeout: source.oidc_timeout ?? 30,
      oidc_verify_audience: source.oidc_verify_audience ?? true,
      oidc_audience: source.oidc_audience ?? '',
      oidc_use_pkce: source.oidc_use_pkce ?? true,
      oidc_verify_issuer: source.oidc_verify_issuer ?? true
    };
  }

  let formData: OIDCConfig = buildFormData(config);

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

  /**
   * Purely a UI convenience — not a saved setting. Selecting a preset writes
   * into the same `oidc_roles_claim` / `oidc_scopes` / `oidc_discovery_url`
   * fields an admin would otherwise type by hand, then the selection itself is
   * discarded; nothing new is persisted.
   */
  let selectedPresetId = '';

  $: selectedPreset = selectedPresetId ? findOIDCPreset(selectedPresetId) : undefined;

  /**
   * Reads the id off the event target rather than trusting `selectedPresetId` /
   * `selectedPreset` to already be flushed: both are `bind:value` / `$:`-derived,
   * and Svelte reruns reactive statements on its own schedule, not synchronously
   * within the `change` handler that set them.
   */
  function applyPreset(event: Event) {
    const presetId = (event.currentTarget as HTMLSelectElement).value;
    selectedPresetId = presetId;
    const preset = findOIDCPreset(presetId);
    if (!preset) return;
    const values = oidcPresetFieldValues(preset);
    formData.oidc_roles_claim = values.oidc_roles_claim;
    formData.oidc_scopes = values.oidc_scopes;
    if (values.oidc_discovery_url !== undefined) {
      formData.oidc_discovery_url = values.oidc_discovery_url;
    }
    handleChange();
  }

  $: if (config) {
    formData = buildFormData(config);
    // A reload means the previous edit either landed or was discarded; either way
    // the typed secret must not survive into the next save.
    clientSecretInput = '';
  }

  $: clientSecretIsSet = secretIsSet ?? config.oidc_client_secret === null;

  /** A discovery URL supersedes the realm-based endpoint construction. */
  $: discoveryActive = (formData.oidc_discovery_url ?? '').trim() !== '';

  /**
   * An empty allow-list admits **everyone** the provider authenticates. That is
   * the entire safety story of this control, and it is the state a deployment
   * ships in — so it is stated on screen permanently, not buried in help text
   * nobody reads until the whole tenant has accounts.
   */
  $: allowListOpen = (formData.oidc_allowed_groups ?? '').trim() === '';

  /** Claim names (never values) the provider's discovery document advertised. */
  $: discoveredClaims = (testResult?.success && testResult.details?.claims_supported) || undefined;
  $: rolesClaimAdvertised = testResult?.success
    ? (testResult.details?.roles_claim_advertised as string | undefined)
    : undefined;

  /** Payload for save/test: everything in the form, plus the secret only if typed. */
  function buildPayload(): OIDCConfig {
    return clientSecretInput === ''
      ? { ...formData }
      : { ...formData, oidc_client_secret: clientSecretInput };
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
      formData.oidc_callback_url = `${window.location.origin}/api/auth/oidc/callback`;
      handleChange();
    }
  }
</script>

<div class="settings-panel">
  <div class="enable-toggle">
    <label class="toggle-label">
      <input
        type="checkbox"
        bind:checked={formData.oidc_enabled}
        on:change={handleChange}
      />
      <span class="toggle-text">{$t('settings.oidc.enable')}</span>
    </label>
  </div>

  <div class="info-box">
    <strong>{$t('settings.oidc.oidcTitle')}</strong>
    <p>{$t('settings.oidc.oidcDescription')}</p>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.serverConfiguration')}</h3>

    <div class="form-group">
      <label for="oidc_provider_preset">{$t('settings.oidc.providerPreset')}</label>
      <select
        id="oidc_provider_preset"
        value={selectedPresetId}
        on:change={applyPreset}
        disabled={!formData.oidc_enabled}
      >
        <option value="">{$t('settings.oidc.presetChoose')}</option>
        {#each OIDC_PROVIDER_PRESETS as preset (preset.id)}
          <option value={preset.id}>{$t(preset.labelKey)}</option>
        {/each}
      </select>
      <span class="help-text">{$t('settings.oidc.providerPresetHelp')}</span>
      {#if selectedPreset?.noteKey}
        <p class="admission-warning" role="status">{$t(selectedPreset.noteKey)}</p>
      {/if}
    </div>

    <div class="form-group">
      <label for="oidc_server_url">{$t('settings.oidc.serverUrlPublic')}</label>
      <input
        id="oidc_server_url"
        type="text"
        bind:value={formData.oidc_server_url}
        on:input={handleChange}
        placeholder="https://idp.example.com"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.serverUrlPublicHelp')}</span>
    </div>

    <div class="form-group">
      <label for="oidc_internal_url">{$t('settings.oidc.serverUrlInternal')}</label>
      <input
        id="oidc_internal_url"
        type="text"
        bind:value={formData.oidc_internal_url}
        on:input={handleChange}
        placeholder="http://idp:8080"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.serverUrlInternalHelp')}</span>
    </div>

    <div class="form-group">
      <label for="oidc_discovery_url">{$t('settings.oidc.discoveryUrl')}</label>
      <input
        id="oidc_discovery_url"
        type="text"
        bind:value={formData.oidc_discovery_url}
        on:input={handleChange}
        placeholder="https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.discoveryUrlHelp')}</span>
    </div>

    <div class="form-group" class:superseded={discoveryActive}>
      <label for="oidc_realm">{$t('settings.oidc.realm')}</label>
      <input
        id="oidc_realm"
        type="text"
        bind:value={formData.oidc_realm}
        on:input={handleChange}
        placeholder="master"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.realmHelp')}</span>
      {#if discoveryActive}
        <span class="help-text superseded-note">{$t('settings.oidc.realmSupersededHelp')}</span>
      {/if}
    </div>

    <div class="form-group">
      <label for="oidc_timeout">{$t('settings.oidc.requestTimeout')}</label>
      <input
        id="oidc_timeout"
        type="number"
        bind:value={formData.oidc_timeout}
        on:input={handleChange}
        min="5"
        max="120"
        disabled={!formData.oidc_enabled}
      />
    </div>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.clientConfiguration')}</h3>

    <div class="form-row">
      <div class="form-group">
        <label for="oidc_client_id">{$t('settings.oidc.clientId')}</label>
        <input
          id="oidc_client_id"
          type="text"
          bind:value={formData.oidc_client_id}
          on:input={handleChange}
          placeholder="opentranscribe"
          disabled={!formData.oidc_enabled}
        />
      </div>

      <div class="form-group">
        <label for="oidc_client_secret">{$t('settings.oidc.clientSecret')}</label>
        <div class="input-with-toggle">
          <input
            id="oidc_client_secret"
            type={showSecret ? 'text' : 'password'}
            bind:value={clientSecretInput}
            on:input={handleChange}
            autocomplete="new-password"
            placeholder={clientSecretIsSet
              ? $t('settings.oidc.clientSecretKeepPlaceholder')
              : $t('settings.oidc.enterClientSecret')}
            disabled={!formData.oidc_enabled}
          />
          <button
            type="button"
            class="toggle-visibility"
            on:click={() => showSecret = !showSecret}
            disabled={!formData.oidc_enabled}
          >
            {showSecret ? $t('common.hide') : $t('common.show')}
          </button>
        </div>
        <span class="help-text">
          {clientSecretIsSet
            ? $t('settings.oidc.clientSecretSetHelp')
            : $t('settings.oidc.clientSecretHelp')}
        </span>
      </div>
    </div>

    <div class="form-group">
      <label for="oidc_callback_url">{$t('settings.oidc.callbackUrl')}</label>
      <div class="input-with-button">
        <input
          id="oidc_callback_url"
          type="text"
          bind:value={formData.oidc_callback_url}
          on:input={handleChange}
          placeholder="https://app.example.com/login"
          disabled={!formData.oidc_enabled}
        />
        <button
          type="button"
          class="btn btn-small"
          on:click={generateCallbackUrl}
          disabled={!formData.oidc_enabled}
        >
          {$t('settings.oidc.autoDetect')}
        </button>
      </div>
      <span class="help-text">{$t('settings.oidc.callbackUrlHelp')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.securityOptions')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.oidc_use_pkce}
        on:change={handleChange}
        disabled={!formData.oidc_enabled}
      />
      <span>{$t('settings.oidc.usePkce')}</span>
    </label>
    <span class="help-text indented">{$t('settings.oidc.usePkceHelp')}</span>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.oidc_verify_issuer}
        on:change={handleChange}
        disabled={!formData.oidc_enabled}
      />
      <span>{$t('settings.oidc.verifyIssuer')}</span>
    </label>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.oidc_verify_audience}
        on:change={handleChange}
        disabled={!formData.oidc_enabled}
      />
      <span>{$t('settings.oidc.verifyAudience')}</span>
    </label>

    {#if formData.oidc_verify_audience}
      <div class="form-group indented">
        <label for="oidc_audience">{$t('settings.oidc.expectedAudience')}</label>
        <input
          id="oidc_audience"
          type="text"
          bind:value={formData.oidc_audience}
          on:input={handleChange}
          placeholder={$t('settings.oidc.expectedAudiencePlaceholder')}
          disabled={!formData.oidc_enabled}
        />
      </div>
    {/if}
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.roleMapping')}</h3>

    <div class="form-group">
      <label for="oidc_admin_role">{$t('settings.oidc.adminRoleName')}</label>
      <input
        id="oidc_admin_role"
        type="text"
        bind:value={formData.oidc_admin_role}
        on:input={handleChange}
        placeholder="admin"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.adminRoleHelp')}</span>
    </div>

    <div class="form-group">
      <label for="oidc_roles_claim">{$t('settings.oidc.rolesClaim')}</label>
      <input
        id="oidc_roles_claim"
        type="text"
        bind:value={formData.oidc_roles_claim}
        on:input={handleChange}
        placeholder="realm_access.roles"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.rolesClaimHelp')}</span>
      <span class="help-text">{$t('settings.oidc.rolesClaimWarning')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.admissionControl')}</h3>
    <p class="section-intro">{$t('settings.oidc.admissionControlHelp')}</p>

    <div class="form-group">
      <label for="oidc_allowed_groups">{$t('settings.oidc.allowedGroups')}</label>
      <input
        id="oidc_allowed_groups"
        type="text"
        bind:value={formData.oidc_allowed_groups}
        on:input={handleChange}
        placeholder="opentranscribe-users;CN=Staff,OU=Groups,DC=example,DC=com"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.allowedGroupsHelp')}</span>
      <span class="help-text">{$t('settings.oidc.groupsSemicolonHelp')}</span>
      {#if allowListOpen}
        <p class="admission-warning" role="status">{$t('settings.oidc.allowedGroupsEmptyWarning')}</p>
      {/if}
    </div>

    <div class="form-group">
      <label for="oidc_blocked_groups">{$t('settings.oidc.blockedGroups')}</label>
      <input
        id="oidc_blocked_groups"
        type="text"
        bind:value={formData.oidc_blocked_groups}
        on:input={handleChange}
        placeholder="contractors;CN=Vendors,OU=Groups,DC=example,DC=com"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.blockedGroupsHelp')}</span>
      <span class="help-text">{$t('settings.oidc.groupsSemicolonHelp')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.advancedOptions')}</h3>

    <div class="form-group">
      <label for="oidc_scopes">{$t('settings.oidc.scopes')}</label>
      <input
        id="oidc_scopes"
        type="text"
        bind:value={formData.oidc_scopes}
        on:input={handleChange}
        placeholder="openid email profile"
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.scopesHelp')}</span>
    </div>

    <div class="form-group">
      <label for="oidc_issuer">{$t('settings.oidc.issuer')}</label>
      <input
        id="oidc_issuer"
        type="text"
        bind:value={formData.oidc_issuer}
        on:input={handleChange}
        placeholder={$t('settings.oidc.issuerPlaceholder')}
        disabled={!formData.oidc_enabled}
      />
      <span class="help-text">{$t('settings.oidc.issuerHelp')}</span>
    </div>
  </div>

  {#if testResult?.success}
    <div class="section discovered-claims">
      <h3>{$t('settings.oidc.discoveredClaims')}</h3>
      <p class="section-intro">{$t('settings.oidc.discoveredClaimsHelp')}</p>

      {#if discoveredClaims}
        <div class="claim-tags">
          {#each discoveredClaims as claim (claim)}
            <span class="claim-tag">{claim}</span>
          {/each}
        </div>
      {:else}
        <p class="help-text">{$t('settings.oidc.noClaimsSupported')}</p>
      {/if}

      {#if formData.oidc_roles_claim}
        <p class="roles-claim-status" class:status-no={rolesClaimAdvertised === 'no'}>
          {#if rolesClaimAdvertised === 'yes'}
            {$t('settings.oidc.rolesClaimAdvertisedYes', { claim: formData.oidc_roles_claim })}
          {:else if rolesClaimAdvertised === 'no'}
            {$t('settings.oidc.rolesClaimAdvertisedNo', { claim: formData.oidc_roles_claim })}
          {:else}
            {$t('settings.oidc.rolesClaimAdvertisedUnknown', { claim: formData.oidc_roles_claim })}
          {/if}
        </p>
      {/if}
    </div>
  {/if}

  <div class="actions">
    <button
      class="btn btn-secondary"
      on:click={handleTest}
      disabled={!formData.oidc_enabled || testing}
    >
      {testing ? $t('common.testing') : $t('settings.oidc.testConnection')}
    </button>
    <button
      class="btn btn-primary"
      on:click={handleSave}
      disabled={saving}
    >
      {saving ? $t('common.saving') : $t('settings.oidc.saveConfiguration')}
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

  .section-intro {
    margin: -0.5rem 0 1rem 0;
    font-size: 0.8125rem;
    color: var(--color-text-secondary);
    line-height: 1.5;
  }

  .admission-warning {
    margin: 0.5rem 0 0 0;
    padding: 0.6rem 0.75rem;
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-radius: 6px;
    background: rgba(245, 158, 11, 0.12);
    color: var(--color-text);
    font-size: 0.75rem;
    line-height: 1.5;
  }

  .claim-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin: 0.5rem 0;
  }

  .claim-tag {
    padding: 0.2rem 0.6rem;
    border-radius: 999px;
    background: var(--color-bg-tertiary);
    border: 1px solid var(--color-border);
    color: var(--color-text);
    font-size: 0.75rem;
    font-family: monospace;
  }

  .roles-claim-status {
    margin: 0.5rem 0 0 0;
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    background: rgba(16, 185, 129, 0.12);
    border: 1px solid rgba(16, 185, 129, 0.4);
    color: var(--color-text);
    font-size: 0.8125rem;
    line-height: 1.5;
  }

  .roles-claim-status.status-no {
    background: rgba(245, 158, 11, 0.12);
    border-color: rgba(245, 158, 11, 0.45);
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
  .form-group input[type="number"],
  .form-group select {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 0.875rem;
  }

  .form-group input:focus,
  .form-group select:focus {
    outline: none;
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px var(--color-primary-alpha);
  }

  .form-group input:disabled,
  .form-group select:disabled {
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
