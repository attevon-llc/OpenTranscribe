<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { GROUP_MAPPING_ROLE_KEYS, labelFor } from '$lib/i18n/keyMaps';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Badge from '$components/ui/Badge.svelte';
  import {
    GroupMappingsApi,
    type GroupMappingSource,
    type MappingTestResponse
  } from '$lib/api/groupMappings';

  export let source: GroupMappingSource = 'ldap';

  /**
   * `claims` pastes the values the provider emits; `username` asks the directory
   * for a real account's groups. The second is LDAP-only — an OIDC provider
   * asserts membership inside a token issued to the user, so there is nothing to
   * look up for somebody else, and the endpoint 400s on the attempt. Rather than
   * let an admin walk into that error, the mode is switched back automatically.
   */
  let mode: 'claims' | 'username' = 'claims';
  $: if (source !== 'ldap' && mode === 'username') mode = 'claims';

  let claimsText = '';
  let username = '';
  let running = false;
  let result: MappingTestResponse | null = null;

  // A source switch invalidates the previous answer — the mappings consulted are
  // a different set. Showing a stale result under the new source would be a lie.
  $: if (source) result = null;

  /**
   * Newlines and semicolons separate values — **never commas**. A directory DN
   * (`CN=Legal-Team,OU=Groups,DC=example,DC=com`) is full of commas, so a
   * comma-split turns one group into four fragments that match nothing, and the
   * test then reports "unmatched" about a claim that resolves perfectly well.
   * This is the same reason `oidc_allowed_groups` is semicolon-delimited.
   */
  $: parsedClaims = claimsText
    .split(/[\n;]/)
    .map((value) => value.trim())
    .filter((value) => value !== '');

  $: canRun =
    !running && (mode === 'username' ? username.trim() !== '' : parsedClaims.length > 0);

  async function runTest() {
    if (!canRun) return;
    running = true;
    try {
      result = await GroupMappingsApi.test(
        mode === 'username'
          ? { source, username: username.trim() }
          : { source, claim_values: parsedClaims }
      );
    } catch (err: unknown) {
      result = null;
      toastStore.error(getErrorMessage(err, $t('settings.groupMappings.test.failed')));
    } finally {
      running = false;
    }
  }
</script>

<div class="tester">
  <div class="tester-head">
    <h4>{$t('settings.groupMappings.test.title')}</h4>
    <Badge variant="info">{$t('settings.groupMappings.test.readOnlyBadge')}</Badge>
  </div>
  <p class="tester-note">{$t('settings.groupMappings.test.readOnlyNote')}</p>

  <div class="mode-row" role="radiogroup" aria-label={$t('settings.groupMappings.test.modeLabel')}>
    <label class="radio-label">
      <input type="radio" bind:group={mode} value="claims" />
      <span>{$t('settings.groupMappings.test.modeClaims')}</span>
    </label>
    <label class="radio-label" class:unavailable={source !== 'ldap'}>
      <input type="radio" bind:group={mode} value="username" disabled={source !== 'ldap'} />
      <span>{$t('settings.groupMappings.test.modeUsername')}</span>
    </label>
  </div>
  {#if source !== 'ldap'}
    <span class="help-text">{$t('settings.groupMappings.test.usernameLdapOnly')}</span>
  {/if}

  {#if mode === 'claims'}
    <FormField class="tester-field" id="mapping-test-claims" label={$t('settings.groupMappings.test.claimsLabel')}>
      <textarea dir="ltr"
        id="mapping-test-claims"
        class="form-control form-textarea"
        bind:value={claimsText}
        rows="3"
        placeholder={source === 'ldap'
          ? $t('settings.groupMappings.claimValuePlaceholderLdap')
          : $t('settings.groupMappings.claimValuePlaceholderOidc')}
      ></textarea>
      <span class="help-text">{$t('settings.groupMappings.test.claimsHelp')}</span>
    </FormField>
  {:else}
    <FormField class="tester-field" id="mapping-test-username" label={$t('settings.groupMappings.test.usernameLabel')}>
      <input
        id="mapping-test-username"
        type="text"
        class="form-control"
        bind:value={username}
        maxlength="255"
        placeholder={$t('settings.groupMappings.test.usernamePlaceholder')}
      />
      <span class="help-text">{$t('settings.groupMappings.test.usernameHelp')}</span>
    </FormField>
  {/if}

  <div class="tester-actions">
    <button type="button" class="btn btn-secondary" on:click={runTest} disabled={!canRun}>
      {running ? $t('settings.groupMappings.test.running') : $t('settings.groupMappings.test.run')}
    </button>
  </div>

  {#if result}
    <div class="result" aria-live="polite">
      <div class="result-row">
        <span class="result-label">{$t('settings.groupMappings.test.effectiveRole')}</span>
        <Badge variant={result.effective_role === 'admin' ? 'warning' : 'default'}>
          {labelFor($t, GROUP_MAPPING_ROLE_KEYS, result.effective_role)}
        </Badge>
      </div>

      <div class="result-row">
        <span class="result-label">{$t('settings.groupMappings.test.groups')}</span>
        {#if result.groups.length}
          <span class="chips">
            {#each result.groups as group (group.uuid)}
              <Badge variant="success">{group.name}</Badge>
            {/each}
          </span>
        {:else}
          <span class="muted">{$t('settings.groupMappings.test.noGroups')}</span>
        {/if}
      </div>

      <div class="result-row">
        <span class="result-label">{$t('settings.groupMappings.test.matched')}</span>
        {#if result.matched_claims.length}
          <!-- Keyed by position: a pasted list may legitimately repeat a value, and a
               claim-string key throws each_key_duplicate. The chips are never reordered. -->
          <span class="chips">
            {#each result.matched_claims as claim, i (i)}
              <Badge variant="info">{claim}</Badge>
            {/each}
          </span>
        {:else}
          <span class="muted">{$t('settings.groupMappings.test.none')}</span>
        {/if}
      </div>

      <div class="result-row">
        <span class="result-label">{$t('settings.groupMappings.test.unmatched')}</span>
        {#if result.unmatched_claims.length}
          <span class="chips">
            {#each result.unmatched_claims as claim, i (i)}
              <Badge variant="default">{claim}</Badge>
            {/each}
          </span>
        {:else}
          <span class="muted">{$t('settings.groupMappings.test.none')}</span>
        {/if}
      </div>

      {#if result.grants_role}
        <p class="result-note">
          {$t('settings.groupMappings.test.roleFromMapping', {
            role: labelFor($t, GROUP_MAPPING_ROLE_KEYS, result.grants_role)
          })}
        </p>
      {/if}

      {#if result.legacy_admin}
        <!-- The subject is an admin through ldap_admin_groups / ldap_admin_users,
             independently of any mapping. Without saying so the panel would report
             "no mapping grants admin" about an account that still lands as one. -->
        <p class="result-note warn">{$t('settings.groupMappings.test.legacyAdminNote')}</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .tester {
    margin-top: var(--space-6);
    padding: var(--space-4);
    background: var(--color-bg-secondary);
    border-radius: var(--radius-lg);
  }

  .tester-head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: 0.35rem;
  }

  .tester-head h4 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--color-text-secondary);
  }

  .tester-note {
    margin: 0 0 var(--space-3) 0;
    font-size: var(--font-size-xs);
    color: var(--color-text-tertiary);
    line-height: 1.5;
  }

  .mode-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-5);
    margin-bottom: var(--space-2);
  }

  .radio-label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: var(--font-size-sm);
    cursor: pointer;
    /* Without this the flex row shrinks each label to its longest word and
       stacks "Paste / claim / values" vertically. */
    white-space: nowrap;
  }

  .radio-label.unavailable {
    opacity: 0.5;
    cursor: not-allowed;
  }



  :global(.form-field.tester-field) {
    margin: var(--space-3) 0;
  }

  .form-control {
    width: 100%;
  }

  .form-textarea {
    resize: vertical;
  }

  .help-text {
    display: block;
    font-size: var(--font-size-xs);
    color: var(--color-text-tertiary);
    line-height: 1.45;
  }

  .tester-actions {
    display: flex;
    justify-content: flex-end;
  }

  .result {
    margin-top: var(--space-4);
    padding: 0.85rem;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    background: var(--color-bg);
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .result-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .result-label {
    min-width: 9rem;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--color-text-secondary);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  .muted {
    font-size: var(--font-size-sm);
    color: var(--color-text-tertiary);
  }

  .result-note {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--color-text-secondary);
    line-height: 1.5;
  }

  .result-note.warn {
    color: var(--color-warning-text);
  }

  @media (max-width: 768px) {
    .result-label {
      min-width: 100%;
    }
  }
</style>
