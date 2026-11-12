<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { OIDCConfig, AuthMethodTestResponse } from '$lib/api/authConfig';
  import { OIDC_FORM_DEFAULTS } from '$lib/settings/authFormDefaults';
  import { withDefaults } from '$lib/utils/withDefaults';
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
    return withDefaults(OIDC_FORM_DEFAULTS, source);
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
  $: claimsSupported = testResult?.success ? testResult.details?.claims_supported : undefined;
  $: discoveredClaims = Array.isArray(claimsSupported) ? claimsSupported.map(String) : undefined;
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

  function handleTest() {
    testing = true;
    dispatch('test', buildPayload());
    setTimeout(() => testing = false, 2000);
  }

  /**
   * The SPA route the identity provider must send the browser back to.
   *
   * This value is used twice by `auth/oidc/flow.py`: as the `redirect_uri` in the
   * authorization request (so the IdP *navigates the browser* to it) and again at
   * token exchange, where it must match byte for byte. It therefore has to be a page
   * the **frontend** serves. `/api/auth/oidc/callback` is not: that handler returns a
   * `JSONResponse` and never redirects, so registering it authenticates the user
   * successfully and then strands them on a raw JSON document with no way forward.
   *
   * `/login` reads `?code`/`?state` on mount and XHRs the backend callback itself.
   */
  const CALLBACK_PATH = '/login';

  function generateCallbackUrl() {
    if (typeof window !== 'undefined') {
      formData.oidc_callback_url = `${window.location.origin}${CALLBACK_PATH}`;
      handleChange();
    }
  }

  /** The pathname of a typed callback URL, or undefined while it isn't yet a URL. */
  function callbackUrlPath(value: string): string | undefined {
    const trimmed = value.trim();
    if (trimmed === '') return undefined;
    try {
      return new URL(trimmed).pathname;
    } catch {
      // Mid-typing. Nothing useful to say about "https:/" yet.
      return undefined;
    }
  }

  $: callbackPath = callbackUrlPath(formData.oidc_callback_url ?? '');

  /**
   * Warnings, never a block. Auto-detect is the happy path, but an admin can still
   * paste anything here and the mistake stays silent until a real user tries to sign
   * in — by which time the fix needs a change at the provider too. Two levels because
   * the confidence differs: a backend `/api/` route is *knowably* broken, whereas a
   * reverse proxy serving the SPA from a renamed or nested route is unusual but
   * legitimate, and the browser cannot tell that apart from a typo. Hard-blocking the
   * second case would make a working deployment unconfigurable.
   */
  $: callbackUrlIsApiRoute = callbackPath !== undefined && callbackPath.split('/').includes('api');
  $: callbackUrlUnexpectedPath =
    callbackPath !== undefined && !callbackUrlIsApiRoute && !callbackPath.endsWith(CALLBACK_PATH);
</script>

<div class="settings-panel auth-panel">
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

  <div class="settings-info-box">
    <strong>{$t('settings.oidc.oidcTitle')}</strong>
    <p>{$t('settings.oidc.oidcDescription')}</p>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.serverConfiguration')}</h3>

    <FormField size="lg" id="oidc_provider_preset" label={$t('settings.oidc.providerPreset')} hint={$t('settings.oidc.providerPresetHelp')} let:describedby>
      <select
        id="oidc_provider_preset"
        aria-describedby={describedby}
        value={selectedPresetId}
        on:change={applyPreset}
        disabled={!formData.oidc_enabled}
      >
        <option value="">{$t('settings.oidc.presetChoose')}</option>
        {#each OIDC_PROVIDER_PRESETS as preset (preset.id)}
          <option value={preset.id}>{$t(preset.labelKey)}</option>
        {/each}
      </select>
      <svelte:fragment slot="after-hint">
        {#if selectedPreset?.noteKey}
          <p class="admission-warning" role="status">{$t(selectedPreset.noteKey)}</p>
        {/if}
      </svelte:fragment>
    </FormField>

    <FormField size="lg" id="oidc_server_url" label={$t('settings.oidc.serverUrlPublic')} hint={$t('settings.oidc.serverUrlPublicHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_server_url" aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_server_url}
        on:input={handleChange}
        placeholder="https://idp.example.com"
        disabled={!formData.oidc_enabled}
      />
    </FormField>

    <FormField size="lg" id="oidc_internal_url" label={$t('settings.oidc.serverUrlInternal')} hint={$t('settings.oidc.serverUrlInternalHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_internal_url" aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_internal_url}
        on:input={handleChange}
        placeholder="http://idp:8080"
        disabled={!formData.oidc_enabled}
      />
    </FormField>

    <FormField size="lg" id="oidc_discovery_url" label={$t('settings.oidc.discoveryUrl')} hint={$t('settings.oidc.discoveryUrlHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_discovery_url" aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_discovery_url}
        on:input={handleChange}
        placeholder="https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration"
        disabled={!formData.oidc_enabled}
      />
    </FormField>

    <FormField size="lg" class={discoveryActive ? 'superseded' : ''} id="oidc_realm" label={$t('settings.oidc.realm')} hint={$t('settings.oidc.realmHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_realm"
        aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_realm}
        on:input={handleChange}
        placeholder="master"
        disabled={!formData.oidc_enabled}
      />
      <svelte:fragment slot="after-hint">
        {#if discoveryActive}
          <p class="form-field-hint superseded-note">{$t('settings.oidc.realmSupersededHelp')}</p>
        {/if}
      </svelte:fragment>
    </FormField>

    <FormField size="lg" id="oidc_timeout" label={$t('settings.oidc.requestTimeout')}>
      <input
        id="oidc_timeout"
        type="number"
        bind:value={formData.oidc_timeout}
        on:input={handleChange}
        min="5"
        max="120"
        disabled={!formData.oidc_enabled}
      />
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.clientConfiguration')}</h3>

    <div class="form-row">
      <FormField size="lg" id="oidc_client_id" label={$t('settings.oidc.clientId')}>
        <input dir="ltr"
          id="oidc_client_id"
          type="text"
          bind:value={formData.oidc_client_id}
          on:input={handleChange}
          placeholder="opentranscribe"
          disabled={!formData.oidc_enabled}
        />
      </FormField>

      <FormField size="lg" id="oidc_client_secret" label={$t('settings.oidc.clientSecret')} hint={clientSecretIsSet ? $t('settings.oidc.clientSecretSetHelp') : $t('settings.oidc.clientSecretHelp')} let:describedby>
        <div class="input-with-toggle">
          <input dir="ltr"
            id="oidc_client_secret"
            aria-describedby={describedby}
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
      </FormField>
    </div>

    <FormField size="lg" id="oidc_callback_url" label={$t('settings.oidc.callbackUrl')} hint={$t('settings.oidc.callbackUrlHelp')} let:describedby>
      <div class="input-with-button">
        <input dir="ltr"
          id="oidc_callback_url"
        aria-describedby={describedby}
          type="text"
          bind:value={formData.oidc_callback_url}
          on:input={handleChange}
          placeholder="https://app.example.com/login"
          disabled={!formData.oidc_enabled}
        />
        <button
          type="button"
          class="btn btn-primary btn-sm"
          on:click={generateCallbackUrl}
          disabled={!formData.oidc_enabled}
        >
          {$t('settings.oidc.autoDetect')}
        </button>
      </div>
      <svelte:fragment slot="after-hint">
        {#if callbackUrlIsApiRoute}
          <p class="admission-warning" role="status">{$t('settings.oidc.callbackUrlApiWarning')}</p>
        {:else if callbackUrlUnexpectedPath}
          <p class="admission-warning" role="status">{$t('settings.oidc.callbackUrlPathWarning')}</p>
        {/if}
      </svelte:fragment>
    </FormField>
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
      <FormField size="lg" class="indented" id="oidc_audience" label={$t('settings.oidc.expectedAudience')} let:describedby>
        <input dir="ltr"
          id="oidc_audience"
          aria-describedby={describedby}
          type="text"
          bind:value={formData.oidc_audience}
          on:input={handleChange}
          placeholder={$t('settings.oidc.expectedAudiencePlaceholder')}
          disabled={!formData.oidc_enabled}
        />
      </FormField>
    {/if}
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.roleMapping')}</h3>

    <FormField size="lg" id="oidc_admin_role" label={$t('settings.oidc.adminRoleName')} hint={$t('settings.oidc.adminRoleHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_admin_role" aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_admin_role}
        on:input={handleChange}
        placeholder="admin"
        disabled={!formData.oidc_enabled}
      />
    </FormField>

    <FormField size="lg" id="oidc_roles_claim" label={$t('settings.oidc.rolesClaim')} hint={$t('settings.oidc.rolesClaimHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_roles_claim"
        aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_roles_claim}
        on:input={handleChange}
        placeholder="realm_access.roles"
        disabled={!formData.oidc_enabled}
      />
      <svelte:fragment slot="after-hint">
        <p class="form-field-hint">{$t('settings.oidc.rolesClaimWarning')}</p>
      </svelte:fragment>
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.admissionControl')}</h3>
    <p class="section-intro">{$t('settings.oidc.admissionControlHelp')}</p>

    <FormField size="lg" id="oidc_allowed_groups" label={$t('settings.oidc.allowedGroups')} hint={$t('settings.oidc.allowedGroupsHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_allowed_groups"
        aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_allowed_groups}
        on:input={handleChange}
        placeholder="opentranscribe-users;CN=Staff,OU=Groups,DC=example,DC=com"
        disabled={!formData.oidc_enabled}
      />
      <svelte:fragment slot="after-hint">
        <p class="form-field-hint">{$t('settings.oidc.groupsSemicolonHelp')}</p>
        {#if allowListOpen}
          <p class="admission-warning" role="status">{$t('settings.oidc.allowedGroupsEmptyWarning')}</p>
        {/if}
      </svelte:fragment>
    </FormField>

    <FormField size="lg" id="oidc_blocked_groups" label={$t('settings.oidc.blockedGroups')} hint={$t('settings.oidc.blockedGroupsHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_blocked_groups"
        aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_blocked_groups}
        on:input={handleChange}
        placeholder="contractors;CN=Vendors,OU=Groups,DC=example,DC=com"
        disabled={!formData.oidc_enabled}
      />
      <svelte:fragment slot="after-hint">
        <p class="form-field-hint">{$t('settings.oidc.groupsSemicolonHelp')}</p>
      </svelte:fragment>
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.oidc_enabled}>
    <h3>{$t('settings.oidc.advancedOptions')}</h3>

    <FormField size="lg" id="oidc_scopes" label={$t('settings.oidc.scopes')} hint={$t('settings.oidc.scopesHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_scopes" aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_scopes}
        on:input={handleChange}
        placeholder="openid email profile"
        disabled={!formData.oidc_enabled}
      />
    </FormField>

    <FormField size="lg" id="oidc_issuer" label={$t('settings.oidc.issuer')} hint={$t('settings.oidc.issuerHelp')} let:describedby>
      <input dir="ltr"
        id="oidc_issuer" aria-describedby={describedby}
        type="text"
        bind:value={formData.oidc_issuer}
        on:input={handleChange}
        placeholder={$t('settings.oidc.issuerPlaceholder')}
        disabled={!formData.oidc_enabled}
      />
    </FormField>
  </div>

  {#if testResult?.success}
    <div class="section discovered-claims">
      <h3>{$t('settings.oidc.discoveredClaims')}</h3>
      <p class="section-intro">{$t('settings.oidc.discoveredClaimsHelp')}</p>

      {#if discoveredClaims}
        <div class="claim-tags">
          {#each discoveredClaims as claim, i (i)}
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

  .section-intro {
    margin: calc(-1 * var(--space-2)) 0 var(--space-4) 0;
    font-size: var(--font-size-sm);
    color: var(--color-text-secondary);
    line-height: 1.5;
  }

  .admission-warning {
    margin: var(--space-2) 0 0 0;
    padding: var(--space-2-5) var(--space-3);
    border: 1px solid rgba(var(--warning-color-rgb), 0.45);
    border-radius: var(--radius-md);
    background: rgba(var(--warning-color-rgb), 0.12);
    color: var(--color-text);
    font-size: var(--font-size-xs);
    line-height: 1.5;
  }

  .claim-tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
    margin: var(--space-2) 0;
  }

  .claim-tag {
    padding: var(--space-1) var(--space-2-5);
    border-radius: var(--radius-pill);
    background: var(--color-bg-tertiary);
    border: 1px solid var(--color-border);
    color: var(--color-text);
    font-size: var(--font-size-xs);
    font-family: monospace;
  }

  .roles-claim-status {
    margin: var(--space-2) 0 0 0;
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    background: rgba(var(--success-color-rgb), 0.12);
    border: 1px solid rgba(var(--success-color-rgb), 0.4);
    color: var(--color-text);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .roles-claim-status.status-no {
    background: rgba(var(--warning-color-rgb), 0.12);
    border-color: rgba(var(--warning-color-rgb), 0.45);
  }

  .section :global(.form-field.indented) {
    margin-inline-start: var(--space-6);
  }

  /*
   * A discovery URL makes the realm irrelevant. Fade it rather than disable it —
   * an admin may be mid-edit, and a disabled field also loses its value on save.
   */
  .section :global(.form-field.superseded label),
  .section :global(.form-field.superseded input) {
    opacity: 0.55;
  }

  .section :global(.form-field) {
    flex: 1;
    margin-bottom: var(--space-4);
  }

  /*
   * A discovery URL makes the realm irrelevant. Fade it rather than disable it —
   * an admin may be mid-edit, and a disabled field also loses its value on save.
   */

  .superseded-note {
    color: var(--color-warning-text);
  }

  .section :global(.form-field input[type="text"]),
  .section :global(.form-field input[type="password"]),
  .section :global(.form-field input[type="number"]),
  .section :global(.form-field select) {
    width: 100%;
  }

  .section :global(.form-field input:disabled),
  .section :global(.form-field select:disabled) {
    cursor: not-allowed;
  }

  .help-text.indented {
    margin-inline-start: var(--space-6);
    margin-bottom: var(--space-3);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    font-size: var(--font-size-md);
    margin-bottom: var(--space-2);
  }

  .actions .btn-secondary {
    margin-inline-end: auto;
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
      margin-inline-end: 0;
    }

    .actions .btn {
      flex: 1;
      min-height: 44px;
    }
  }
</style>
