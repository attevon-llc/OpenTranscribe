<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { buildEmailConfigPayload, type EmailConfigForm } from '$lib/settings/emailConfigPayload';
  import {
    createEmailConfig,
    updateEmailConfig,
    type EmailConfig,
    type EmailProvider,
  } from '$lib/api/watchSourcesApi';

  export let show = false;
  export let editingConfig: EmailConfig | null = null;

  const dispatch = createEventDispatcher();

  let saving = false;
  let lastId: string | null = null;

  function blank(): EmailConfigForm {
    return {
      name: '',
      provider: 'smtp' as EmailProvider,
      is_enabled: true,
      from_address: '',
      default_recipients: '',
      smtp_host: '',
      smtp_port: 587,
      smtp_use_tls: true,
      smtp_username: '',
      smtp_password: '',
      m365_tenant_id: '',
      m365_client_id: '',
      m365_client_secret: '',
      exchange_server: '',
      exchange_domain: '',
      exchange_username: '',
      exchange_password: '',
    };
  }

  let form: EmailConfigForm = blank();

  function formFromConfig(config: EmailConfig) {
    return {
      ...blank(),
      name: config.name,
      provider: config.provider,
      is_enabled: config.is_enabled,
      from_address: config.from_address ?? '',
      default_recipients: config.default_recipients ?? '',
      smtp_host: config.smtp_host ?? '',
      smtp_port: config.smtp_port ?? 587,
      smtp_use_tls: config.smtp_use_tls,
      smtp_username: config.smtp_username ?? '',
      m365_tenant_id: config.m365_tenant_id ?? '',
      m365_client_id: config.m365_client_id ?? '',
      exchange_server: config.exchange_server ?? '',
      exchange_domain: config.exchange_domain ?? '',
      exchange_username: config.exchange_username ?? '',
    };
  }

  function syncEditingConfig(isShown: boolean, config: typeof editingConfig) {
    if (!isShown) return;
    if (config && config.uuid !== lastId) {
      form = formFromConfig(config);
      lastId = config.uuid;
    } else if (!config && lastId !== null) {
      form = blank();
      lastId = null;
    }
  }
  $: syncEditingConfig(show, editingConfig);

  $: isValid = (() => {
    if (!form.name?.trim() || !form.from_address?.trim()) return false;
    if (form.provider === 'smtp') return !!form.smtp_host?.trim();
    if (form.provider === 'm365') return !!(form.m365_tenant_id?.trim() && form.m365_client_id?.trim());
    if (form.provider === 'exchange') return !!form.exchange_server?.trim();
    return false;
  })();

  function buildPayload() {
    return buildEmailConfigPayload(form);
  }

  async function handleSave() {
    if (!isValid) return;
    saving = true;
    try {
      if (editingConfig) {
        const updated = await updateEmailConfig(editingConfig.uuid, buildPayload());
        toastStore.success($t('settings.emailNotifications.saved', { name: updated.name }));
        dispatch('saved', updated);
      } else {
        const created = await createEmailConfig({ ...buildPayload(), provider: form.provider });
        toastStore.success($t('settings.emailNotifications.saved', { name: created.name }));
        dispatch('saved', created);
      }
      dispatch('close');
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.emailNotifications.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<BaseModal isOpen={show} onClose={() => dispatch('close')} size="md">
  <svelte:fragment slot="header">
    <h2 class="modal-title">
      {editingConfig
        ? $t('settings.emailNotifications.editTitle')
        : $t('settings.emailNotifications.addTitle')}
    </h2>
    <span class="experimental-badge" title={$t('settings.emailNotifications.experimentalNote')}>
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M9 3h6M10 3v6.5L5.2 18a2 2 0 0 0 1.8 3h10a2 2 0 0 0 1.8-3L14 9.5V3" />
        <path d="M7.5 14h9" />
      </svg>
      {$t('settings.emailNotifications.experimental')}
    </span>
  </svelte:fragment>

  <div class="email-form">
    <FormField id="em-name" label={$t('settings.emailNotifications.fields.name')} labelSize="md">
      <input id="em-name" type="text" class="form-input" bind:value={form.name} />
    </FormField>
    <FormField id="em-provider" label={$t('settings.emailNotifications.fields.provider')} labelSize="md">
      <select id="em-provider" class="form-select" bind:value={form.provider} disabled={!!editingConfig}>
        <option value="smtp">SMTP</option>
        <option value="m365">Microsoft 365</option>
        <option value="exchange">Exchange (on-prem)</option>
      </select>
    </FormField>

    {#if form.provider === 'smtp'}
      <p class="provider-help">{$t('settings.emailNotifications.smtpHelp')}</p>
    {:else if form.provider === 'm365'}
      <p class="provider-help">{$t('settings.emailNotifications.m365Help')}</p>
    {:else if form.provider === 'exchange'}
      <p class="provider-help">{$t('settings.emailNotifications.exchangeHelp')}</p>
    {/if}

    <div class="form-row">
      <FormField id="em-from" label={$t('settings.emailNotifications.fields.fromAddress')} labelSize="md">
        <input dir="ltr" id="em-from" type="email" class="form-input" bind:value={form.from_address} />
      </FormField>
      <FormField id="em-rcpt" label={$t('settings.emailNotifications.fields.defaultRecipients')} labelSize="md">
        <input dir="ltr" id="em-rcpt" type="text" class="form-input" bind:value={form.default_recipients} placeholder="a@x.com, b@x.com" />
      </FormField>
    </div>

    {#if form.provider === 'smtp'}
      <div class="form-row">
        <FormField id="em-host" label={$t('settings.emailNotifications.fields.smtpHost')} labelSize="md">
          <input dir="ltr" id="em-host" type="text" class="form-input" bind:value={form.smtp_host} />
        </FormField>
        <FormField id="em-port" label={$t('settings.emailNotifications.fields.smtpPort')} labelSize="md">
          <input id="em-port" type="number" class="form-input" bind:value={form.smtp_port} />
        </FormField>
      </div>
      <FormField id="em-user" label={$t('settings.emailNotifications.fields.smtpUsername')} labelSize="md">
        <input id="em-user" type="text" class="form-input" bind:value={form.smtp_username} />
      </FormField>
      <FormField id="em-pass" label={$t('settings.emailNotifications.fields.smtpPassword')} labelSize="md">
        <input dir="ltr" id="em-pass" type="password" class="form-input" bind:value={form.smtp_password} placeholder={editingConfig?.has_smtp_password ? $t('settings.watchSources.secretStored') : ''} />
      </FormField>
      <label class="checkbox-row">
        <input type="checkbox" bind:checked={form.smtp_use_tls} />
        <span>{$t('settings.emailNotifications.fields.useTls')}</span>
      </label>
    {:else if form.provider === 'm365'}
      <FormField id="em-tenant" label={$t('settings.emailNotifications.fields.tenantId')} labelSize="md">
        <input dir="ltr" id="em-tenant" type="text" class="form-input" bind:value={form.m365_tenant_id} />
      </FormField>
      <FormField id="em-client" label={$t('settings.emailNotifications.fields.clientId')} labelSize="md">
        <input dir="ltr" id="em-client" type="text" class="form-input" bind:value={form.m365_client_id} />
      </FormField>
      <FormField id="em-secret" label={$t('settings.emailNotifications.fields.clientSecret')} labelSize="md">
        <input dir="ltr" id="em-secret" type="password" class="form-input" bind:value={form.m365_client_secret} placeholder={editingConfig?.has_m365_secret ? $t('settings.watchSources.secretStored') : ''} />
      </FormField>
    {:else if form.provider === 'exchange'}
      <div class="form-row">
        <FormField id="em-exsrv" label={$t('settings.emailNotifications.fields.exchangeServer')} labelSize="md">
          <input dir="ltr" id="em-exsrv" type="text" class="form-input" bind:value={form.exchange_server} />
        </FormField>
        <FormField id="em-export" label={$t('settings.emailNotifications.fields.smtpPort')} labelSize="md">
          <input id="em-export" type="number" class="form-input" bind:value={form.smtp_port} />
        </FormField>
      </div>
      <div class="form-row">
        <FormField id="em-exdom" label={$t('settings.emailNotifications.fields.exchangeDomain')} labelSize="md">
          <input dir="ltr" id="em-exdom" type="text" class="form-input" bind:value={form.exchange_domain} />
        </FormField>
        <FormField id="em-exuser" label={$t('settings.emailNotifications.fields.exchangeUsername')} labelSize="md">
          <input id="em-exuser" type="text" class="form-input" bind:value={form.exchange_username} />
        </FormField>
      </div>
      <FormField id="em-expass" label={$t('settings.emailNotifications.fields.exchangePassword')} labelSize="md">
        <input id="em-expass" type="password" class="form-input" bind:value={form.exchange_password} placeholder={editingConfig?.has_exchange_password ? $t('settings.watchSources.secretStored') : ''} />
      </FormField>
    {/if}

    <label class="checkbox-row">
      <input type="checkbox" bind:checked={form.is_enabled} />
      <span>{$t('settings.emailNotifications.fields.enabled')}</span>
    </label>
  </div>

  <svelte:fragment slot="footer">
    <button class="btn btn-secondary" on:click={() => dispatch('close')}>{$t('common.cancel')}</button>
    <button class="btn btn-primary" on:click={handleSave} disabled={saving || !isValid}>
      {saving ? $t('common.saving') : editingConfig ? $t('common.update') : $t('common.save')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .email-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .modal-title {
    margin: 0;
    font-size: 1.1rem;
  }
  .experimental-badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    margin-left: var(--space-2);
    font-size: 0.68rem;
    font-weight: var(--font-weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    background: rgba(234, 179, 8, 0.15);
    color: #b45309;
  }
  :global([data-theme='dark']) .experimental-badge {
    color: #fbbf24;
  }
  .form-row > :global(.form-field) {
    flex: 1;
  }
  .form-row {
    display: flex;
    gap: var(--space-3);
  }
  /* .form-input / .form-select inherit the global input/select styling. */
  .provider-help {
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.5;
    margin: 0;
    padding: var(--space-2) var(--space-2-5);
    background: var(--button-hover);
    border-radius: var(--radius-md);
  }
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: 0.9rem;
    cursor: pointer;
  }
  /* Override the global `input { width:100% }` base so checkboxes stay square. */
  .checkbox-row input[type='checkbox'] {
    width: 16px;
    height: 16px;
    min-height: 0;
    margin: 0;
    padding: 0;
    flex: none;
    cursor: pointer;
    accent-color: var(--primary-color);
  }
</style>
