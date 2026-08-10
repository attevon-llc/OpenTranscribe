<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { SAMLConfig } from '$lib/api/authConfig';
  import { toastStore } from '$stores/toast';
  import { copyToClipboard } from '$lib/utils/clipboard';

  export let config: Partial<SAMLConfig> = {};

  /**
   * Whether the backend reports a stored SP private key (`is_set` on the
   * sensitive config row). The key itself never reaches the browser, so this
   * is the only signal available for the "leave blank to keep the current
   * value" affordance. Falls back to `config.saml_sp_private_key === null`,
   * which is what a stored sensitive row flattens to.
   */
  export let privateKeyIsSet: boolean | undefined = undefined;

  const dispatch = createEventDispatcher();

  /**
   * `saml_sp_private_key` is deliberately absent — see `privateKeyInput`.
   */
  function buildFormData(source: Partial<SAMLConfig>): Omit<SAMLConfig, 'saml_sp_private_key'> {
    return {
      saml_enabled: source.saml_enabled ?? false,
      saml_sp_entity_id: source.saml_sp_entity_id ?? '',
      saml_sp_acs_url: source.saml_sp_acs_url ?? '',
      saml_sp_sls_url: source.saml_sp_sls_url ?? '',
      saml_sp_x509_cert: source.saml_sp_x509_cert ?? '',
      saml_idp_entity_id: source.saml_idp_entity_id ?? '',
      saml_idp_sso_url: source.saml_idp_sso_url ?? '',
      saml_idp_slo_url: source.saml_idp_slo_url ?? '',
      saml_idp_x509_cert: source.saml_idp_x509_cert ?? '',
      saml_want_assertions_signed: source.saml_want_assertions_signed ?? true,
      saml_want_messages_signed: source.saml_want_messages_signed ?? true,
      saml_sign_authn_requests: source.saml_sign_authn_requests ?? false,
      saml_email_attribute:
        source.saml_email_attribute ??
        'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress',
      saml_name_attribute:
        source.saml_name_attribute ?? 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name',
      saml_groups_attribute: source.saml_groups_attribute ?? 'groups',
      saml_admin_group: source.saml_admin_group ?? '',
      saml_allowed_groups: source.saml_allowed_groups ?? '',
      saml_blocked_groups: source.saml_blocked_groups ?? ''
    };
  }

  let formData = buildFormData(config);

  /**
   * Held outside `formData` so the private key is only ever submitted when
   * the admin typed one. Never bind the API's placeholder into the field —
   * the next save would encrypt the placeholder over the real secret.
   */
  let privateKeyInput = '';

  let saving = false;
  let showPrivateKey = false;

  $: if (config) {
    formData = buildFormData(config);
    // A reload means the previous edit either landed or was discarded; the
    // typed key must not survive into the next save either way.
    privateKeyInput = '';
  }

  $: privateKeySet = privateKeyIsSet ?? config.saml_sp_private_key === null;

  /** Same empty-admits-everyone semantics as OIDC/Proxy admission control. */
  $: allowListOpen = (formData.saml_allowed_groups ?? '').trim() === '';

  $: metadataUrl = typeof window !== 'undefined' ? `${window.location.origin}/api/auth/saml/metadata` : '';

  function buildPayload(): Record<string, unknown> {
    const payload: Record<string, unknown> = { ...formData };
    if (privateKeyInput !== '') {
      payload.saml_sp_private_key = privateKeyInput;
    }
    return payload;
  }

  function handleChange() {
    dispatch('change');
  }

  function handleSave() {
    saving = true;
    dispatch('save', buildPayload());
    setTimeout(() => (saving = false), 500);
  }

  async function copyMetadataUrl() {
    if (!metadataUrl) return;
    await copyToClipboard(
      metadataUrl,
      () => toastStore.success($t('settings.saml.metadataUrlCopied')),
      () => toastStore.error($t('settings.saml.metadataUrlCopyFailed'))
    );
  }
</script>

<div class="settings-panel">
  <div class="enable-toggle">
    <label class="toggle-label">
      <input type="checkbox" bind:checked={formData.saml_enabled} on:change={handleChange} />
      <span class="toggle-text">{$t('settings.saml.enable')}</span>
    </label>
  </div>

  <div class="info-box">
    <strong>{$t('settings.saml.samlTitle')}</strong>
    <p>{$t('settings.saml.samlDescription')}</p>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.spIdentity')}</h3>

    <div class="form-group">
      <label for="saml_sp_entity_id">{$t('settings.saml.spEntityId')}</label>
      <input
        id="saml_sp_entity_id"
        type="text"
        bind:value={formData.saml_sp_entity_id}
        on:input={handleChange}
        placeholder="https://app.example.com/saml/metadata"
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.spEntityIdHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_sp_acs_url">{$t('settings.saml.spAcsUrl')}</label>
      <input
        id="saml_sp_acs_url"
        type="text"
        bind:value={formData.saml_sp_acs_url}
        on:input={handleChange}
        placeholder="https://app.example.com/api/auth/saml/acs"
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.spAcsUrlHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_sp_sls_url">{$t('settings.saml.spSlsUrl')}</label>
      <input
        id="saml_sp_sls_url"
        type="text"
        bind:value={formData.saml_sp_sls_url}
        on:input={handleChange}
        placeholder="https://app.example.com/api/auth/saml/sls"
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.spSlsUrlHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_metadata_url">{$t('settings.saml.metadataUrl')}</label>
      <div class="input-with-button">
        <input id="saml_metadata_url" type="text" value={metadataUrl} readonly />
        <button type="button" class="btn btn-small" on:click={copyMetadataUrl}>
          {$t('settings.saml.copy')}
        </button>
      </div>
      <span class="help-text">{$t('settings.saml.metadataUrlHelp')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.spCertificate')}</h3>

    <div class="form-group">
      <label for="saml_sp_x509_cert">{$t('settings.saml.spX509Cert')}</label>
      <textarea
        id="saml_sp_x509_cert"
        bind:value={formData.saml_sp_x509_cert}
        on:input={handleChange}
        placeholder="-----BEGIN CERTIFICATE-----"
        rows="4"
        disabled={!formData.saml_enabled}
      ></textarea>
      <span class="help-text">{$t('settings.saml.spX509CertHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_sp_private_key">{$t('settings.saml.spPrivateKey')}</label>
      <div class="input-with-toggle">
        <input
          id="saml_sp_private_key"
          type={showPrivateKey ? 'text' : 'password'}
          bind:value={privateKeyInput}
          on:input={handleChange}
          autocomplete="new-password"
          placeholder={privateKeySet
            ? $t('settings.saml.spPrivateKeyKeepPlaceholder')
            : $t('settings.saml.enterSpPrivateKey')}
          disabled={!formData.saml_enabled}
        />
        <button
          type="button"
          class="toggle-visibility"
          on:click={() => (showPrivateKey = !showPrivateKey)}
          disabled={!formData.saml_enabled}
        >
          {showPrivateKey ? $t('common.hide') : $t('common.show')}
        </button>
      </div>
      <span class="help-text">
        {privateKeySet
          ? $t('settings.saml.spPrivateKeySetHelp')
          : $t('settings.saml.spPrivateKeyHelp')}
      </span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.idpIdentity')}</h3>

    <div class="form-group">
      <label for="saml_idp_entity_id">{$t('settings.saml.idpEntityId')}</label>
      <input
        id="saml_idp_entity_id"
        type="text"
        bind:value={formData.saml_idp_entity_id}
        on:input={handleChange}
        placeholder="https://idp.example.com/metadata"
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.idpEntityIdHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_idp_sso_url">{$t('settings.saml.idpSsoUrl')}</label>
      <input
        id="saml_idp_sso_url"
        type="text"
        bind:value={formData.saml_idp_sso_url}
        on:input={handleChange}
        placeholder="https://idp.example.com/sso"
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.idpSsoUrlHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_idp_slo_url">{$t('settings.saml.idpSloUrl')}</label>
      <input
        id="saml_idp_slo_url"
        type="text"
        bind:value={formData.saml_idp_slo_url}
        on:input={handleChange}
        placeholder="https://idp.example.com/slo"
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.idpSloUrlHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_idp_x509_cert">{$t('settings.saml.idpX509Cert')}</label>
      <textarea
        id="saml_idp_x509_cert"
        bind:value={formData.saml_idp_x509_cert}
        on:input={handleChange}
        placeholder="-----BEGIN CERTIFICATE-----"
        rows="4"
        disabled={!formData.saml_enabled}
      ></textarea>
      <span class="help-text">{$t('settings.saml.idpX509CertHelp')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.securityPosture')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.saml_want_assertions_signed}
        on:change={handleChange}
        disabled={!formData.saml_enabled}
      />
      <span>{$t('settings.saml.wantAssertionsSigned')}</span>
    </label>
    <span class="help-text indented">{$t('settings.saml.wantAssertionsSignedHelp')}</span>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.saml_want_messages_signed}
        on:change={handleChange}
        disabled={!formData.saml_enabled}
      />
      <span>{$t('settings.saml.wantMessagesSigned')}</span>
    </label>
    <span class="help-text indented">{$t('settings.saml.wantMessagesSignedHelp')}</span>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.saml_sign_authn_requests}
        on:change={handleChange}
        disabled={!formData.saml_enabled}
      />
      <span>{$t('settings.saml.signAuthnRequests')}</span>
    </label>
    <span class="help-text indented">{$t('settings.saml.signAuthnRequestsHelp')}</span>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.attributeMapping')}</h3>

    <div class="form-group">
      <label for="saml_email_attribute">{$t('settings.saml.emailAttribute')}</label>
      <input
        id="saml_email_attribute"
        type="text"
        bind:value={formData.saml_email_attribute}
        on:input={handleChange}
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.emailAttributeHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_name_attribute">{$t('settings.saml.nameAttribute')}</label>
      <input
        id="saml_name_attribute"
        type="text"
        bind:value={formData.saml_name_attribute}
        on:input={handleChange}
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.nameAttributeHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_groups_attribute">{$t('settings.saml.groupsAttribute')}</label>
      <input
        id="saml_groups_attribute"
        type="text"
        bind:value={formData.saml_groups_attribute}
        on:input={handleChange}
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.groupsAttributeHelp')}</span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.admissionControl')}</h3>

    <div class="form-group">
      <label for="saml_admin_group">{$t('settings.saml.adminGroup')}</label>
      <input
        id="saml_admin_group"
        type="text"
        bind:value={formData.saml_admin_group}
        on:input={handleChange}
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.adminGroupHelp')}</span>
    </div>

    <div class="form-group">
      <label for="saml_allowed_groups">{$t('settings.saml.allowedGroups')}</label>
      <input
        id="saml_allowed_groups"
        type="text"
        bind:value={formData.saml_allowed_groups}
        on:input={handleChange}
        placeholder="opentranscribe-users;CN=Staff,OU=Groups,DC=example,DC=com"
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.allowedGroupsHelp')}</span>
      <span class="help-text">{$t('settings.saml.groupsSemicolonHelp')}</span>
      {#if allowListOpen}
        <p class="admission-warning" role="status">{$t('settings.saml.allowedGroupsEmptyWarning')}</p>
      {/if}
    </div>

    <div class="form-group">
      <label for="saml_blocked_groups">{$t('settings.saml.blockedGroups')}</label>
      <input
        id="saml_blocked_groups"
        type="text"
        bind:value={formData.saml_blocked_groups}
        on:input={handleChange}
        placeholder="contractors;CN=Vendors,OU=Groups,DC=example,DC=com"
        disabled={!formData.saml_enabled}
      />
      <span class="help-text">{$t('settings.saml.blockedGroupsHelp')}</span>
      <span class="help-text">{$t('settings.saml.groupsSemicolonHelp')}</span>
    </div>
  </div>

  <div class="actions">
    <button class="btn btn-primary" on:click={handleSave} disabled={saving}>
      {saving ? $t('common.saving') : $t('settings.saml.saveConfiguration')}
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

  .form-group {
    flex: 1;
    margin-bottom: 1rem;
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
  .form-group textarea {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 0.875rem;
    font-family: inherit;
  }

  .form-group textarea {
    resize: vertical;
    min-height: 80px;
    font-family: monospace;
    font-size: 0.75rem;
  }

  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px var(--color-primary-alpha);
  }

  .form-group input:disabled,
  .form-group textarea:disabled {
    background: var(--color-bg-tertiary);
    cursor: not-allowed;
  }

  .form-group input[readonly] {
    background: var(--color-bg-tertiary);
    color: var(--color-text-secondary);
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
    white-space: nowrap;
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
</style>
