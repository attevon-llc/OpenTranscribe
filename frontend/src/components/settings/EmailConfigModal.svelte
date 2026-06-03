<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
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

  function blank(): any {
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

  let form: any = blank();

  $: if (show) {
    if (editingConfig && editingConfig.uuid !== lastId) {
      form = {
        ...blank(),
        name: editingConfig.name,
        provider: editingConfig.provider,
        is_enabled: editingConfig.is_enabled,
        from_address: editingConfig.from_address ?? '',
        default_recipients: editingConfig.default_recipients ?? '',
        smtp_host: editingConfig.smtp_host ?? '',
        smtp_port: editingConfig.smtp_port ?? 587,
        smtp_use_tls: editingConfig.smtp_use_tls,
        smtp_username: editingConfig.smtp_username ?? '',
        m365_tenant_id: editingConfig.m365_tenant_id ?? '',
        m365_client_id: editingConfig.m365_client_id ?? '',
        exchange_server: editingConfig.exchange_server ?? '',
        exchange_domain: editingConfig.exchange_domain ?? '',
        exchange_username: editingConfig.exchange_username ?? '',
      };
      lastId = editingConfig.uuid;
    } else if (!editingConfig && lastId !== null) {
      form = blank();
      lastId = null;
    }
  }

  $: isValid = (() => {
    if (!form.name?.trim() || !form.from_address?.trim()) return false;
    if (form.provider === 'smtp') return !!form.smtp_host?.trim();
    if (form.provider === 'm365') return !!(form.m365_tenant_id?.trim() && form.m365_client_id?.trim());
    if (form.provider === 'exchange') return !!form.exchange_server?.trim();
    return false;
  })();

  function buildPayload(): any {
    const p: any = {
      name: form.name.trim(),
      is_enabled: form.is_enabled,
      from_address: form.from_address.trim(),
      default_recipients: form.default_recipients?.trim() || null,
    };
    if (form.provider === 'smtp') {
      p.smtp_host = form.smtp_host.trim();
      p.smtp_port = Number(form.smtp_port);
      p.smtp_use_tls = form.smtp_use_tls;
      p.smtp_username = form.smtp_username?.trim() || null;
      if (form.smtp_password?.trim()) p.smtp_password = form.smtp_password.trim();
    } else if (form.provider === 'm365') {
      p.m365_tenant_id = form.m365_tenant_id.trim();
      p.m365_client_id = form.m365_client_id.trim();
      if (form.m365_client_secret?.trim()) p.m365_client_secret = form.m365_client_secret.trim();
    } else if (form.provider === 'exchange') {
      p.exchange_server = form.exchange_server.trim();
      p.exchange_domain = form.exchange_domain?.trim() || null;
      p.exchange_username = form.exchange_username?.trim() || null;
      p.smtp_port = Number(form.smtp_port);
      if (form.exchange_password?.trim()) p.exchange_password = form.exchange_password.trim();
    }
    return p;
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

<BaseModal isOpen={show} onClose={() => dispatch('close')} maxWidth="560px">
  <svelte:fragment slot="header">
    <h2 class="modal-title">
      {editingConfig
        ? $t('settings.emailNotifications.editTitle')
        : $t('settings.emailNotifications.addTitle')}
    </h2>
  </svelte:fragment>

  <div class="email-form">
    <div class="form-group">
      <label for="em-name">{$t('settings.emailNotifications.fields.name')}</label>
      <input id="em-name" type="text" class="form-input" bind:value={form.name} />
    </div>
    <div class="form-group">
      <label for="em-provider">{$t('settings.emailNotifications.fields.provider')}</label>
      <select id="em-provider" class="form-select" bind:value={form.provider} disabled={!!editingConfig}>
        <option value="smtp">SMTP</option>
        <option value="m365">Microsoft 365</option>
        <option value="exchange">Exchange (on-prem)</option>
      </select>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label for="em-from">{$t('settings.emailNotifications.fields.fromAddress')}</label>
        <input id="em-from" type="email" class="form-input" bind:value={form.from_address} />
      </div>
      <div class="form-group">
        <label for="em-rcpt">{$t('settings.emailNotifications.fields.defaultRecipients')}</label>
        <input id="em-rcpt" type="text" class="form-input" bind:value={form.default_recipients} placeholder="a@x.com, b@x.com" />
      </div>
    </div>

    {#if form.provider === 'smtp'}
      <div class="form-row">
        <div class="form-group">
          <label for="em-host">{$t('settings.emailNotifications.fields.smtpHost')}</label>
          <input id="em-host" type="text" class="form-input" bind:value={form.smtp_host} />
        </div>
        <div class="form-group">
          <label for="em-port">{$t('settings.emailNotifications.fields.smtpPort')}</label>
          <input id="em-port" type="number" class="form-input" bind:value={form.smtp_port} />
        </div>
      </div>
      <div class="form-group">
        <label for="em-user">{$t('settings.emailNotifications.fields.smtpUsername')}</label>
        <input id="em-user" type="text" class="form-input" bind:value={form.smtp_username} />
      </div>
      <div class="form-group">
        <label for="em-pass">{$t('settings.emailNotifications.fields.smtpPassword')}</label>
        <input id="em-pass" type="password" class="form-input" bind:value={form.smtp_password} placeholder={editingConfig?.has_smtp_password ? $t('settings.watchSources.secretStored') : ''} />
      </div>
      <label class="checkbox-row">
        <input type="checkbox" bind:checked={form.smtp_use_tls} />
        <span>{$t('settings.emailNotifications.fields.useTls')}</span>
      </label>
    {:else if form.provider === 'm365'}
      <div class="form-group">
        <label for="em-tenant">{$t('settings.emailNotifications.fields.tenantId')}</label>
        <input id="em-tenant" type="text" class="form-input" bind:value={form.m365_tenant_id} />
      </div>
      <div class="form-group">
        <label for="em-client">{$t('settings.emailNotifications.fields.clientId')}</label>
        <input id="em-client" type="text" class="form-input" bind:value={form.m365_client_id} />
      </div>
      <div class="form-group">
        <label for="em-secret">{$t('settings.emailNotifications.fields.clientSecret')}</label>
        <input id="em-secret" type="password" class="form-input" bind:value={form.m365_client_secret} placeholder={editingConfig?.has_m365_secret ? $t('settings.watchSources.secretStored') : ''} />
      </div>
    {:else if form.provider === 'exchange'}
      <div class="form-row">
        <div class="form-group">
          <label for="em-exsrv">{$t('settings.emailNotifications.fields.exchangeServer')}</label>
          <input id="em-exsrv" type="text" class="form-input" bind:value={form.exchange_server} />
        </div>
        <div class="form-group">
          <label for="em-export">{$t('settings.emailNotifications.fields.smtpPort')}</label>
          <input id="em-export" type="number" class="form-input" bind:value={form.smtp_port} />
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label for="em-exdom">{$t('settings.emailNotifications.fields.exchangeDomain')}</label>
          <input id="em-exdom" type="text" class="form-input" bind:value={form.exchange_domain} />
        </div>
        <div class="form-group">
          <label for="em-exuser">{$t('settings.emailNotifications.fields.exchangeUsername')}</label>
          <input id="em-exuser" type="text" class="form-input" bind:value={form.exchange_username} />
        </div>
      </div>
      <div class="form-group">
        <label for="em-expass">{$t('settings.emailNotifications.fields.exchangePassword')}</label>
        <input id="em-expass" type="password" class="form-input" bind:value={form.exchange_password} placeholder={editingConfig?.has_exchange_password ? $t('settings.watchSources.secretStored') : ''} />
      </div>
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
    gap: 12px;
  }
  .modal-title {
    margin: 0;
    font-size: 1.1rem;
  }
  .form-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
  }
  .form-row {
    display: flex;
    gap: 12px;
  }
  .form-group label {
    font-size: 0.85rem;
    color: var(--text-secondary);
  }
  .form-input,
  .form-select {
    padding: 8px 10px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 0.9rem;
  }
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.9rem;
    cursor: pointer;
  }
</style>
