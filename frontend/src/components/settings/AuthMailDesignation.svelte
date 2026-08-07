<script lang="ts">
  /**
   * Which email configuration carries authentication mail (super_admin).
   *
   * Nothing is auto-selected server-side: these provider rows are created for
   * specific notification purposes, and mailing password resets out of an
   * unrelated mailbox is both a credential leak and a deliverability problem. So
   * the designation is an explicit act, and this panel is where it happens.
   *
   * The backend reports whether the designation still *resolves* — a row deleted
   * or disabled after it was designated leaves auth mail silently falling back to
   * the env SMTP transport, which is unset in a stock deployment. That case is
   * surfaced here rather than left to a log line nobody reads.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { AuthConfigApi, type AuthMailDesignation } from '$lib/api/authConfig';
  import type { EmailConfig } from '$lib/api/watchSourcesApi';

  export let configs: EmailConfig[] = [];

  let designation: AuthMailDesignation | null = null;
  let selected = '';
  let loading = true;
  let saving = false;

  /** Only an enabled config can be designated — the API rejects the rest. */
  $: selectable = configs.filter((c) => c.is_enabled);
  $: dangling =
    designation !== null && (designation.status === 'missing' || designation.status === 'disabled');
  /**
   * A dangling designation names a row that is not in `selectable`, so without
   * this the select would render blank and look like "nothing designated".
   */
  $: strandedUuid = dangling ? (designation?.config_uuid ?? '') : '';
  $: dirty = designation !== null && selected !== (designation.config_uuid ?? '');
  $: undeliverable = designation !== null && !designation.resolves && !designation.env_smtp_configured;

  async function load() {
    loading = true;
    try {
      designation = await AuthConfigApi.getAuthMailDesignation();
      selected = designation.config_uuid ?? '';
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.authentication.authMail.loadFailed')));
    } finally {
      loading = false;
    }
  }

  onMount(load);

  async function save() {
    saving = true;
    try {
      designation = await AuthConfigApi.setAuthMailDesignation(selected);
      selected = designation.config_uuid ?? '';
      toastStore.success(
        designation.config_name
          ? $t('settings.authentication.authMail.saved', { name: designation.config_name })
          : $t('settings.authentication.authMail.cleared')
      );
    } catch (err) {
      // The 400 body names what is wrong with the chosen config; show it verbatim.
      toastStore.error(getErrorMessage(err, $t('settings.authentication.authMail.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<div class="auth-mail">
  <h5>{$t('settings.authentication.authMail.heading')}</h5>
  <p class="auth-mail-help">{$t('settings.authentication.authMail.description')}</p>

  {#if !loading}
    <div class="auth-mail-row">
      <div class="form-group">
        <label for="auth-mail-config">{$t('settings.authentication.authMail.label')}</label>
        <select id="auth-mail-config" class="form-select" bind:value={selected} disabled={saving}>
          <option value="">{$t('settings.authentication.authMail.none')}</option>
          {#each selectable as c (c.uuid)}
            <option value={c.uuid}>{c.name} ({c.provider.toUpperCase()})</option>
          {/each}
          {#if strandedUuid}
            <option value={strandedUuid}>
              {designation?.config_name
                ? $t('settings.authentication.authMail.disabledOption', {
                    name: designation.config_name,
                  })
                : $t('settings.authentication.authMail.missingOption', {
                    uuid: strandedUuid,
                  })}
            </option>
          {/if}
        </select>
      </div>
      <button class="btn btn-primary" on:click={save} disabled={saving || !dirty}>
        {saving ? $t('common.saving') : $t('common.save')}
      </button>
    </div>

    {#if dangling}
      <p class="auth-mail-warning" role="alert">
        {designation?.status === 'disabled'
          ? $t('settings.authentication.authMail.danglingDisabled', {
              name: designation?.config_name ?? '',
            })
          : $t('settings.authentication.authMail.danglingMissing')}
      </p>
    {/if}
    {#if undeliverable}
      <p class="auth-mail-warning" role="alert">
        {$t('settings.authentication.authMail.noTransport')}
      </p>
    {/if}
    {#if designation?.resolves}
      <p class="auth-mail-active">
        {$t('settings.authentication.authMail.active', {
          name: designation?.config_name ?? '',
        })}
      </p>
    {/if}
  {/if}
</div>

<style>
  .auth-mail {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 16px;
  }
  .auth-mail h5 {
    margin: 0;
    font-size: 0.95rem;
  }
  .auth-mail-help {
    margin: 0;
    font-size: 0.8rem;
    line-height: 1.5;
    color: var(--text-secondary);
  }
  .auth-mail-row {
    display: flex;
    align-items: flex-end;
    gap: 12px;
  }
  .form-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
  }
  .form-group label {
    font-size: 0.85rem;
    color: var(--text-secondary);
  }
  /* Both banners theme themselves from the shared tokens, so light and dark
     stay in parity without a second rule set. */
  .auth-mail-warning {
    margin: 0;
    padding: 8px 10px;
    border: 1px solid var(--warning-border);
    border-radius: 6px;
    background: var(--warning-bg);
    color: var(--text-color);
    font-size: 0.8rem;
    line-height: 1.45;
  }
  .auth-mail-active {
    margin: 0;
    font-size: 0.8rem;
    color: var(--success-color);
  }
</style>
