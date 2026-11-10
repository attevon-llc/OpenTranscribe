<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { SAMLConfig } from '$lib/api/authConfig';
  import { SAML_FORM_DEFAULTS } from '$lib/settings/authFormDefaults';
  import { withDefaults } from '$lib/utils/withDefaults';
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
    return withDefaults(SAML_FORM_DEFAULTS, source);
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

  const metadataUrl = typeof window !== 'undefined' ? `${window.location.origin}/api/auth/saml/metadata` : '';

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

    <FormField size="lg" id="saml_sp_entity_id" label={$t('settings.saml.spEntityId')} hint={$t('settings.saml.spEntityIdHelp')} let:describedby>
      <input dir="ltr"
        id="saml_sp_entity_id" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_sp_entity_id}
        on:input={handleChange}
        placeholder="https://app.example.com/saml/metadata"
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_sp_acs_url" label={$t('settings.saml.spAcsUrl')} hint={$t('settings.saml.spAcsUrlHelp')} let:describedby>
      <input dir="ltr"
        id="saml_sp_acs_url" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_sp_acs_url}
        on:input={handleChange}
        placeholder="https://app.example.com/api/auth/saml/acs"
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_sp_sls_url" label={$t('settings.saml.spSlsUrl')} hint={$t('settings.saml.spSlsUrlHelp')} let:describedby>
      <input dir="ltr"
        id="saml_sp_sls_url" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_sp_sls_url}
        on:input={handleChange}
        placeholder="https://app.example.com/api/auth/saml/sls"
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_metadata_url" label={$t('settings.saml.metadataUrl')} hint={$t('settings.saml.metadataUrlHelp')} let:describedby>
      <div class="input-with-button">
        <input dir="ltr" id="saml_metadata_url" aria-describedby={describedby} type="text" value={metadataUrl} readonly />
        <button type="button" class="btn btn-small" on:click={copyMetadataUrl}>
          {$t('settings.saml.copy')}
        </button>
      </div>
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.spCertificate')}</h3>

    <FormField size="lg" id="saml_sp_x509_cert" label={$t('settings.saml.spX509Cert')} hint={$t('settings.saml.spX509CertHelp')} let:describedby>
      <textarea dir="ltr"
        id="saml_sp_x509_cert" aria-describedby={describedby}
        bind:value={formData.saml_sp_x509_cert}
        on:input={handleChange}
        placeholder="-----BEGIN CERTIFICATE-----"
        rows="4"
        disabled={!formData.saml_enabled}
      ></textarea>
    </FormField>

    <FormField size="lg" id="saml_sp_private_key" label={$t('settings.saml.spPrivateKey')} hint={privateKeySet ? $t('settings.saml.spPrivateKeySetHelp') : $t('settings.saml.spPrivateKeyHelp')} let:describedby>
      <div class="input-with-toggle">
        <input dir="ltr"
          id="saml_sp_private_key"
        aria-describedby={describedby}
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
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.idpIdentity')}</h3>

    <FormField size="lg" id="saml_idp_entity_id" label={$t('settings.saml.idpEntityId')} hint={$t('settings.saml.idpEntityIdHelp')} let:describedby>
      <input dir="ltr"
        id="saml_idp_entity_id" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_idp_entity_id}
        on:input={handleChange}
        placeholder="https://idp.example.com/metadata"
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_idp_sso_url" label={$t('settings.saml.idpSsoUrl')} hint={$t('settings.saml.idpSsoUrlHelp')} let:describedby>
      <input dir="ltr"
        id="saml_idp_sso_url" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_idp_sso_url}
        on:input={handleChange}
        placeholder="https://idp.example.com/sso"
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_idp_slo_url" label={$t('settings.saml.idpSloUrl')} hint={$t('settings.saml.idpSloUrlHelp')} let:describedby>
      <input dir="ltr"
        id="saml_idp_slo_url" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_idp_slo_url}
        on:input={handleChange}
        placeholder="https://idp.example.com/slo"
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_idp_x509_cert" label={$t('settings.saml.idpX509Cert')} hint={$t('settings.saml.idpX509CertHelp')} let:describedby>
      <textarea dir="ltr"
        id="saml_idp_x509_cert" aria-describedby={describedby}
        bind:value={formData.saml_idp_x509_cert}
        on:input={handleChange}
        placeholder="-----BEGIN CERTIFICATE-----"
        rows="4"
        disabled={!formData.saml_enabled}
      ></textarea>
    </FormField>
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

    <FormField size="lg" id="saml_email_attribute" label={$t('settings.saml.emailAttribute')} hint={$t('settings.saml.emailAttributeHelp')} let:describedby>
      <input dir="ltr"
        id="saml_email_attribute" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_email_attribute}
        on:input={handleChange}
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_name_attribute" label={$t('settings.saml.nameAttribute')} hint={$t('settings.saml.nameAttributeHelp')} let:describedby>
      <input dir="ltr"
        id="saml_name_attribute" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_name_attribute}
        on:input={handleChange}
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_groups_attribute" label={$t('settings.saml.groupsAttribute')} hint={$t('settings.saml.groupsAttributeHelp')} let:describedby>
      <input dir="ltr"
        id="saml_groups_attribute" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_groups_attribute}
        on:input={handleChange}
        disabled={!formData.saml_enabled}
      />
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.saml_enabled}>
    <h3>{$t('settings.saml.admissionControl')}</h3>

    <FormField size="lg" id="saml_admin_group" label={$t('settings.saml.adminGroup')} hint={$t('settings.saml.adminGroupHelp')} let:describedby>
      <input dir="ltr"
        id="saml_admin_group" aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_admin_group}
        on:input={handleChange}
        disabled={!formData.saml_enabled}
      />
    </FormField>

    <FormField size="lg" id="saml_allowed_groups" label={$t('settings.saml.allowedGroups')} hint={$t('settings.saml.allowedGroupsHelp')} let:describedby>
      <input dir="ltr"
        id="saml_allowed_groups"
        aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_allowed_groups}
        on:input={handleChange}
        placeholder="opentranscribe-users;CN=Staff,OU=Groups,DC=example,DC=com"
        disabled={!formData.saml_enabled}
      />
      <svelte:fragment slot="after-hint">
        <p class="form-field-hint">{$t('settings.saml.groupsSemicolonHelp')}</p>
        {#if allowListOpen}
          <p class="admission-warning" role="status">{$t('settings.saml.allowedGroupsEmptyWarning')}</p>
        {/if}
      </svelte:fragment>
    </FormField>

    <FormField size="lg" id="saml_blocked_groups" label={$t('settings.saml.blockedGroups')} hint={$t('settings.saml.blockedGroupsHelp')} let:describedby>
      <input dir="ltr"
        id="saml_blocked_groups"
        aria-describedby={describedby}
        type="text"
        bind:value={formData.saml_blocked_groups}
        on:input={handleChange}
        placeholder="contractors;CN=Vendors,OU=Groups,DC=example,DC=com"
        disabled={!formData.saml_enabled}
      />
      <svelte:fragment slot="after-hint">
        <p class="form-field-hint">{$t('settings.saml.groupsSemicolonHelp')}</p>
      </svelte:fragment>
    </FormField>
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

  .info-box {
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    margin-bottom: var(--space-6);
  }

  .info-box strong {
    color: var(--color-text);
  }

  .info-box p {
    margin: var(--space-2) 0 0 0;
    font-size: var(--font-size-md);
    color: var(--color-text-secondary);
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

  .section :global(.form-field) {
    flex: 1;
    margin-bottom: var(--space-4);
  }

  .section :global(.form-field input[type="text"]),
  .section :global(.form-field input[type="password"]),
  .section :global(.form-field textarea) {
    width: 100%;
  }

  .section :global(.form-field textarea) {
    resize: vertical;
    min-height: 80px;
    font-family: monospace;
    font-size: var(--font-size-xs);
  }

  .section :global(.form-field input:disabled),
  .section :global(.form-field textarea:disabled) {
    cursor: not-allowed;
  }

  .input-with-toggle,
  .input-with-button {
    display: flex;
    gap: var(--space-2);
  }

  .input-with-toggle input,
  .input-with-button input {
    flex: 1;
  }

  .toggle-visibility {
    padding: var(--space-2) var(--space-3);
    background: var(--color-bg-tertiary);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
    color: var(--color-text-secondary);
    font-size: var(--font-size-xs);
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
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-xs);
    white-space: nowrap;
  }

  .help-text {
    display: block;
    margin-top: var(--space-1);
    font-size: var(--font-size-xs);
    color: var(--color-text-tertiary);
  }

  .help-text.indented {
    margin-left: var(--space-6);
    margin-bottom: var(--space-3);
  }

  .admission-warning {
    margin: var(--space-2) 0 0 0;
    padding: 0.6rem var(--space-3);
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-radius: var(--radius-md);
    background: rgba(245, 158, 11, 0.12);
    color: var(--color-text);
    font-size: var(--font-size-xs);
    line-height: 1.5;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    font-size: var(--font-size-md);
    margin-bottom: var(--space-2);
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
</style>
