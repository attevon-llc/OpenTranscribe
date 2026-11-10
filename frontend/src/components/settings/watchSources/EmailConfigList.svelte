<script lang="ts">
  /**
   * The deployment-wide email-notification configs (super_admin tier).
   *
   * These are the mailers themselves — creating one holds SMTP/Graph credentials,
   * which is why this block is super_admin. Attaching a mailer to a particular
   * source is a different, owner-level right and lives in
   * `WatchSourceEmailLinksModal.svelte`.
   *
   * Presentational: the coordinator owns the list and every call.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { EmailConfig } from '$lib/api/watchSourcesApi';

  export let configs: EmailConfig[] = [];
  export let showHelp = false;

  const dispatch = createEventDispatcher<{
    create: void;
    edit: EmailConfig;
    test: EmailConfig;
    delete: EmailConfig;
    toggleHelp: void;
  }>();
</script>

<div class="section-head">
  <div class="email-heading">
    <h4>{$t('settings.emailNotifications.heading')}</h4>
    <span class="experimental-badge" title={$t('settings.emailNotifications.experimentalNote')}>
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M9 3h6M10 3v6.5L5.2 18a2 2 0 0 0 1.8 3h10a2 2 0 0 0 1.8-3L14 9.5V3" />
        <path d="M7.5 14h9" />
      </svg>
      {$t('settings.emailNotifications.experimental')}
    </span>
    <button
      class="info-icon"
      type="button"
      aria-expanded={showHelp}
      aria-label={$t('settings.emailNotifications.setupTitle')}
      title={$t('settings.emailNotifications.setupTitle')}
      on:click={() => dispatch('toggleHelp')}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="16" x2="12" y2="12" />
        <line x1="12" y1="8" x2="12.01" y2="8" />
      </svg>
    </button>
  </div>
  <button class="btn btn-primary" on:click={() => dispatch('create')}>
    + {$t('settings.emailNotifications.addConfig')}
  </button>
</div>
<p class="experimental-note">{$t('settings.emailNotifications.experimentalNote')}</p>
{#if showHelp}
  <div class="email-help">
    <h5>{$t('settings.emailNotifications.setupTitle')}</h5>
    <p>
      <strong>SMTP (Gmail, Outlook, Yahoo)</strong> — {$t('settings.emailNotifications.smtpHelp')}
    </p>
    <p><strong>Microsoft 365</strong> — {$t('settings.emailNotifications.m365Help')}</p>
    <p><strong>Exchange</strong> — {$t('settings.emailNotifications.exchangeHelp')}</p>
  </div>
{/if}
{#if configs.length === 0}
  <p class="muted">{$t('settings.emailNotifications.empty')}</p>
{:else}
  <div class="email-list">
    {#each configs as c (c.uuid)}
      <div class="email-card">
        <div>
          <span class="badge type-badge">{c.provider.toUpperCase()}</span>
          <span class="email-name">{c.name}</span>
          <span class="email-from">{c.from_address}</span>
          {#if c.test_status}
            <span class="badge {c.test_status === 'success' ? 'badge-success' : 'badge-error'}">
              {c.test_status}
            </span>
          {/if}
          {#if c.linked_source_count}
            <!-- Deleting this config cascades its links away, so the count is the
                 only warning an admin gets before those sources go quiet. -->
            <span class="badge linked-badge">
              {$t('settings.emailNotifications.links.usedBySources', {
                count: c.linked_source_count,
              })}
            </span>
          {/if}
        </div>
        <div class="email-actions">
          <button class="btn btn-secondary" on:click={() => dispatch('test', c)}>
            {$t('settings.emailNotifications.test')}
          </button>
          <button class="btn btn-secondary" on:click={() => dispatch('edit', c)}>
            {$t('common.edit')}
          </button>
          <button class="btn btn-danger" on:click={() => dispatch('delete', c)}>
            {$t('common.delete')}
          </button>
        </div>
      </div>
    {/each}
  </div>
{/if}

<style>
  .section-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: var(--space-2) 0 var(--space-3);
  }
  .section-head h4 {
    margin: 0;
  }
  .email-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2-5);
  }
  .email-card {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-3-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--surface-color);
  }
  .badge {
    font-size: 0.7rem;
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    font-weight: var(--font-weight-semibold);
  }
  .type-badge {
    background: var(--button-hover);
    color: var(--text-secondary);
  }
  .badge-success {
    background: rgba(34, 197, 94, 0.15);
    color: var(--success-text);
  }
  .badge-error {
    background: rgba(239, 68, 68, 0.15);
    color: var(--error-text);
  }
  .linked-badge {
    background: rgba(var(--primary-color-rgb), 0.15);
    color: var(--primary-on-surface);
    margin-left: var(--space-1-5);
  }
  .email-name {
    font-weight: var(--font-weight-semibold);
    font-size: 0.95rem;
  }
  .email-from {
    color: var(--text-secondary);
    font-size: 0.85rem;
    margin-left: var(--space-2);
  }
  .email-actions {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    flex-wrap: wrap;
  }
  .muted {
    color: var(--text-secondary);
    font-size: 0.85rem;
  }
  .email-heading {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .email-heading h4 {
    margin: 0;
  }
  .experimental-badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
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
  .info-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    box-shadow: none;
    padding: var(--space-0-5);
    cursor: pointer;
    color: var(--text-secondary);
    border-radius: var(--radius-round);
  }
  .info-icon:hover {
    color: var(--primary-on-surface);
    background: var(--button-hover);
    transform: none;
    box-shadow: none;
  }
  .experimental-note {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin: 0 0 var(--space-2-5);
  }
  .email-help {
    border: 1px solid var(--border-color);
    border-left: 3px solid var(--primary-color);
    border-radius: var(--radius-md);
    background: var(--button-hover);
    padding: var(--space-2-5) var(--space-3-5);
    margin-bottom: var(--space-3);
  }
  .email-help h5 {
    margin: 0 0 var(--space-1-5);
    font-size: 0.85rem;
  }
  .email-help p {
    margin: var(--space-1) 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }
</style>
