<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
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
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { AuthConfigApi, type AuthMailDesignation } from '$lib/api/authConfig';
  import { testEmailConfig, type EmailConfig } from '$lib/api/watchSourcesApi';
  import AuthMailNotice from '$components/ui/AuthMailNotice.svelte';

  export let configs: EmailConfig[] = [];

  /** Reports each read/save so the tab strip's attention dot follows what is on screen. */
  const dispatch = createEventDispatcher<{ change: AuthMailDesignation }>();

  let designation: AuthMailDesignation | null = null;
  let selected = '';
  let loading = true;
  let saving = false;
  let testing = false;

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
  /**
   * A dangling designation that env SMTP is quietly covering: mail still goes out,
   * but through a different transport than the one the admin chose. When nothing
   * covers it the shared notice says so instead, naming the same cause.
   */
  $: coveredByEnv = dangling && designation?.deliverable === true;

  async function load() {
    loading = true;
    try {
      designation = await AuthConfigApi.getAuthMailDesignation();
      selected = designation.config_uuid ?? '';
      dispatch('change', designation);
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
      dispatch('change', designation);
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

  /**
   * Reuses the email-config test endpoint (the Watch Sources "Test" button): it checks the
   * connection and credentials, it does not send a message. A failed connection is a 200 with
   * `success: false`, so the outcome is shown either way.
   */
  async function sendTest() {
    if (!designation?.config_uuid) return;
    testing = true;
    try {
      const res = await testEmailConfig(designation.config_uuid);
      if (res.success) toastStore.success(`${designation.config_name ?? ''}: ${res.message}`);
      else toastStore.error(`${designation.config_name ?? ''}: ${res.message}`, 6000);
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.authentication.authMail.testFailed')));
    } finally {
      testing = false;
    }
  }
</script>

<div class="auth-mail">
  <h5>{$t('settings.authentication.authMail.heading')}</h5>
  <p class="auth-mail-help">{$t('settings.authentication.authMail.description')}</p>

  {#if !loading}
    <div class="auth-mail-row">
      <FormField id="auth-mail-config" label={$t('settings.authentication.authMail.label')} labelSize="md">
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
      </FormField>
      <button class="btn btn-primary" on:click={save} disabled={saving || !dirty}>
        {saving ? $t('common.saving') : $t('common.save')}
      </button>
    </div>

    {#if coveredByEnv}
      <p class="auth-mail-warning" role="alert">
        {designation?.status === 'disabled'
          ? $t('settings.authentication.authMail.danglingDisabled', {
              name: designation?.config_name ?? '',
            })
          : $t('settings.authentication.authMail.danglingMissing')}
      </p>
    {/if}
    <AuthMailNotice context="tab" readiness={designation} />
    {#if designation?.resolves}
      <p class="auth-mail-active">
        {$t('settings.authentication.authMail.active', {
          name: designation?.config_name ?? '',
        })}
      </p>
      <div>
        <button
          class="btn btn-secondary"
          data-testid="auth-mail-test"
          on:click={sendTest}
          disabled={testing || saving || dirty}
        >
          {testing
            ? $t('settings.authentication.authMail.testing')
            : $t('settings.authentication.authMail.test')}
        </button>
        <span class="auth-mail-test-help">{$t('settings.authentication.authMail.testHelp')}</span>
      </div>
    {/if}
  {/if}
</div>

<style>
  .auth-mail {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin-bottom: var(--space-4);
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
    gap: var(--space-3);
  }
  /* Both banners theme themselves from the shared tokens, so light and dark
     stay in parity without a second rule set. */
  .auth-mail-warning {
    margin: 0;
    padding: var(--space-2) var(--space-2-5);
    border: 1px solid var(--warning-border);
    border-radius: var(--radius-md);
    background: var(--warning-bg);
    color: var(--text-color);
    font-size: 0.8rem;
    line-height: 1.45;
  }
  .auth-mail-test-help {
    margin-inline-start: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }
  .auth-mail-active {
    margin: 0;
    font-size: 0.8rem;
    color: var(--success-text);
  }
</style>
