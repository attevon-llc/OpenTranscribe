<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { SessionConfig } from '$lib/api/authConfig';

  export let config: Partial<SessionConfig> = {};

  const dispatch = createEventDispatcher();

  let formData: SessionConfig = {
    jwt_access_token_expire_minutes: config.jwt_access_token_expire_minutes ?? 15,
    jwt_refresh_token_expire_days: config.jwt_refresh_token_expire_days ?? 7,
    session_idle_timeout_minutes: config.session_idle_timeout_minutes ?? 30,
    session_absolute_timeout_minutes: config.session_absolute_timeout_minutes ?? 480,
    max_concurrent_sessions: config.max_concurrent_sessions ?? 5,
    // MUST be one of the two literals the backend accepts — see `sessionPolicies`.
    concurrent_session_policy: config.concurrent_session_policy ?? 'terminate_oldest'
  };

  let saving = false;

  $: if (config) {
    formData = {
      jwt_access_token_expire_minutes: config.jwt_access_token_expire_minutes ?? 15,
      jwt_refresh_token_expire_days: config.jwt_refresh_token_expire_days ?? 7,
      session_idle_timeout_minutes: config.session_idle_timeout_minutes ?? 30,
      session_absolute_timeout_minutes: config.session_absolute_timeout_minutes ?? 480,
      max_concurrent_sessions: config.max_concurrent_sessions ?? 5,
      concurrent_session_policy: config.concurrent_session_policy ?? 'terminate_oldest'
    };
  }

  function handleChange() {
    dispatch('change');
  }

  function handleSave() {
    saving = true;
    dispatch('save', formData);
    setTimeout(() => saving = false, 500);
  }

  /**
   * The only two values the backend understands, verbatim:
   * `SessionConfig.concurrent_session_policy` is
   * `Literal["terminate_oldest", "reject"]` (backend/app/schemas/auth_config.py)
   * and `auth_settings.concurrent_session_policy` compares against those strings.
   *
   * This panel used to offer `oldest` / `newest` / `all`, none of which matched —
   * so whichever radio an admin picked, the concurrent-session limit enforced
   * nothing at all. `all` is gone because it has no backend meaning; inventing a
   * third option only recreates the same silent no-op.
   */
  const sessionPolicies = [
    { value: 'terminate_oldest', labelKey: 'settings.session.policyOldestLabel', descKey: 'settings.session.policyOldestDesc' },
    { value: 'reject', labelKey: 'settings.session.policyRejectLabel', descKey: 'settings.session.policyRejectDesc' }
  ];

  function describeMinutes(minutes: number): string {
    if (minutes < 60) return `${minutes} ${$t('settings.session.minutes')}`;
    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    if (remainingMinutes === 0) return `${hours} ${hours > 1 ? $t('settings.session.hours') : $t('settings.session.hour')}`;
    return `${hours}h ${remainingMinutes}m`;
  }

  function formatDays(days: number): string {
    if (days === 1) return `1 ${$t('settings.session.day')}`;
    if (days < 7) return `${days} ${$t('settings.session.days')}`;
    const weeks = Math.floor(days / 7);
    const remainingDays = days % 7;
    if (remainingDays === 0) return `${weeks} ${weeks > 1 ? $t('settings.session.weeks') : $t('settings.session.week')}`;
    return `${weeks}w ${remainingDays}d`;
  }
</script>

<div class="settings-panel">
  <div class="intro-text">
    <p>{$t('settings.session.introText')}</p>
  </div>

  <div class="section">
    <h3>{$t('settings.session.tokenLifetimes')}</h3>

    <!-- The two token lifetimes are the ONLY keys in this panel the API marks
         `requires_restart: true` (auth_config_keys.RESTART_REQUIRED_KEYS):
         app/api/cookies.py reads them at import time to size the session
         cookies, so a live change would desynchronise cookie and token expiry.
         Everything below is enforced against `refresh_token` rows per request
         and really is live. -->
    <div class="form-stack">
    <FormField id="jwt_access_token_expire_minutes" label={$t('settings.session.accessTokenLifetime')} labelSize="md" hint={$t('settings.session.accessTokenHelp')} let:describedby>
      <svelte:fragment slot="label-inline">
        <span class="restart-badge">{$t('settings.session.restartRequired')}</span>
      </svelte:fragment>
      <div class="input-with-preview">
        <input
          id="jwt_access_token_expire_minutes" aria-describedby={describedby}
          type="number"
          bind:value={formData.jwt_access_token_expire_minutes}
          on:input={handleChange}
          min="1"
          max="60"
        />
        <span class="preview">({describeMinutes(formData.jwt_access_token_expire_minutes)})</span>
      </div>
    </FormField>

    <FormField id="jwt_refresh_token_expire_days" label={$t('settings.session.refreshTokenLifetime')} labelSize="md" hint={$t('settings.session.refreshTokenHelp')} let:describedby>
      <svelte:fragment slot="label-inline">
        <span class="restart-badge">{$t('settings.session.restartRequired')}</span>
      </svelte:fragment>
      <div class="input-with-preview">
        <input
          id="jwt_refresh_token_expire_days" aria-describedby={describedby}
          type="number"
          bind:value={formData.jwt_refresh_token_expire_days}
          on:input={handleChange}
          min="1"
          max="90"
        />
        <span class="preview">({formatDays(formData.jwt_refresh_token_expire_days)})</span>
      </div>
    </FormField>
    </div>

    <div class="token-info">
      <strong>{$t('settings.session.howTokensWork')}</strong>
      <ul>
        <li>{$t('settings.session.tokenInfo1')}</li>
        <li>{$t('settings.session.tokenInfo2')}</li>
        <li>{$t('settings.session.tokenInfo3')}</li>
        <li>{$t('settings.session.restartRequiredHelp')}</li>
      </ul>
    </div>
  </div>

  <div class="section">
    <h3>{$t('settings.session.sessionTimeouts')}</h3>

    <div class="form-stack">
    <FormField id="session_idle_timeout_minutes" label={$t('settings.session.idleTimeout')} labelSize="md" hint={$t('settings.session.idleTimeoutHelp')} let:describedby>
      <div class="input-with-preview">
        <input
          id="session_idle_timeout_minutes" aria-describedby={describedby}
          type="number"
          bind:value={formData.session_idle_timeout_minutes}
          on:input={handleChange}
          min="5"
          max="480"
        />
        <span class="preview">({describeMinutes(formData.session_idle_timeout_minutes)})</span>
      </div>
    </FormField>

    <FormField id="session_absolute_timeout_minutes" label={$t('settings.session.absoluteTimeout')} labelSize="md" hint={$t('settings.session.absoluteTimeoutHelp')} let:describedby>
      <div class="input-with-preview">
        <input
          id="session_absolute_timeout_minutes" aria-describedby={describedby}
          type="number"
          bind:value={formData.session_absolute_timeout_minutes}
          on:input={handleChange}
          min="60"
          max="1440"
        />
        <span class="preview">({describeMinutes(formData.session_absolute_timeout_minutes)})</span>
      </div>
    </FormField>
    </div>
  </div>

  <div class="section">
    <h3>{$t('settings.session.concurrentSessions')}</h3>

    <div class="form-stack">
    <FormField id="max_concurrent_sessions" label={$t('settings.session.maxConcurrentSessions')} labelSize="md" hint={$t('settings.session.maxConcurrentSessionsHelp')} let:describedby>
      <input
        id="max_concurrent_sessions" aria-describedby={describedby}
        type="number"
        bind:value={formData.max_concurrent_sessions}
        on:input={handleChange}
        min="1"
        max="20"
      />
    </FormField>

    <fieldset class="form-field form-field-label-md">
      <legend>{$t('settings.session.sessionLimitPolicy')}</legend>
      <div class="radio-group">
        {#each sessionPolicies as policy (policy.value)}
          <label class="radio-label">
            <input
              type="radio"
              name="concurrent_session_policy"
              value={policy.value}
              bind:group={formData.concurrent_session_policy}
              on:change={handleChange}
            />
            <div class="radio-content">
              <span class="radio-title">{$t(policy.labelKey)}</span>
              <span class="radio-description">{$t(policy.descKey)}</span>
            </div>
          </label>
        {/each}
      </div>
    </fieldset>
    </div>
  </div>

  <div class="section info">
    <h3>{$t('settings.session.securityRecommendations')}</h3>
    <div class="recommendations">
      <div class="recommendation">
        <strong>{$t('settings.session.highSecurity')}</strong>
        <p>{$t('settings.session.highSecurityDesc')}</p>
      </div>
      <div class="recommendation">
        <strong>{$t('settings.session.standardSecurity')}</strong>
        <p>{$t('settings.session.standardSecurityDesc')}</p>
      </div>
      <div class="recommendation">
        <strong>{$t('settings.session.convenienceFocused')}</strong>
        <p>{$t('settings.session.convenienceFocusedDesc')}</p>
      </div>
    </div>
  </div>

  <div class="actions">
    <button
      class="btn btn-primary"
      on:click={handleSave}
      disabled={saving}
    >
      {saving ? $t('common.saving') : $t('settings.session.saveConfiguration')}
    </button>
  </div>
</div>

<style>
  .settings-panel {
    max-width: 800px;
  }

  .intro-text {
    margin-bottom: var(--space-6);
    color: var(--color-text-secondary);
    font-size: var(--font-size-md);
  }

  .intro-text p {
    margin: 0;
  }

  .section {
    margin-bottom: var(--space-8);
    padding: var(--space-4);
    background: var(--color-bg-secondary);
    border-radius: var(--radius-lg);
  }

  .section.info {
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
  }

  .section h3 {
    margin: 0 0 var(--space-4) 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--color-text-secondary);
  }



  :global(.form-field) input[type="number"] {
    width: 120px;
  }

  .input-with-preview {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .preview {
    font-size: var(--font-size-md);
    color: var(--color-text-secondary);
  }


  .restart-badge {
    display: inline-block;
    margin-left: var(--space-2);
    padding: var(--space-0-5) var(--space-2);
    border: 1px solid var(--color-warning-border);
    border-radius: var(--radius-pill);
    background: var(--color-warning-bg);
    color: var(--color-warning-text);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    text-transform: none;
    letter-spacing: 0;
    vertical-align: middle;
  }

  .token-info {
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
    padding: var(--space-3);
    margin-top: var(--space-4);
    font-size: var(--font-size-md);
  }

  .token-info strong {
    color: var(--color-text);
    display: block;
    margin-bottom: var(--space-2);
  }

  .token-info ul {
    margin: 0;
    padding-left: var(--space-5);
    color: var(--color-text-secondary);
  }

  .token-info li {
    margin-bottom: var(--space-1);
  }

  .radio-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .radio-label {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    padding: var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    cursor: pointer;
    transition: border-color var(--duration-base), background-color var(--duration-base);
  }

  .radio-label:hover {
    border-color: var(--color-primary);
  }

  .radio-label input[type="radio"] {
    margin-top: var(--space-1);
    width: 1rem;
    height: 1rem;
  }

  .radio-content {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .radio-title {
    font-weight: var(--font-weight-medium);
    color: var(--color-text);
    font-size: var(--font-size-md);
  }

  .radio-description {
    font-size: var(--font-size-xs);
    color: var(--color-text-tertiary);
  }

  .recommendations {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--space-4);
  }

  .recommendation {
    background: var(--color-bg);
    border-radius: var(--radius-sm);
    padding: var(--space-3);
  }

  .recommendation strong {
    display: block;
    margin-bottom: var(--space-1);
    font-size: var(--font-size-md);
    color: var(--color-text);
  }

  .recommendation p {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--color-text-secondary);
  }

  .actions {
    display: flex;
    gap: var(--space-4);
    justify-content: flex-end;
    padding-top: var(--space-4);
    border-top: 1px solid var(--color-border);
  }

  @media (max-width: 768px) {
    .recommendations {
      grid-template-columns: 1fr;
    }

    .actions {
      flex-direction: column-reverse;
    }

    .actions button {
      width: 100%;
      min-height: 44px;
    }
  }
</style>
