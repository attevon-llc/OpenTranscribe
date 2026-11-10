<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { fetchPasswordPolicy, type PasswordPolicy } from '$lib/passwordPolicy';
  import {
    PASSWORD_TIERS,
    effectiveRules,
    isPreset,
    normalizeTier,
    type PasswordTier,
    type PasswordPolicyFormData,
    type PolicyValues,
  } from '$lib/passwordPolicyTiers';

  /** The form object owned by LocalAuthSettings; this panel edits the password_* keys. */
  export let data: PasswordPolicyFormData;
  /** True while local password login is off: the whole panel is inert. */
  export let disabled = false;
  /** Bump to re-read the policy the server enforces (after a save). */
  export let refreshKey = 0;

  const dispatch = createEventDispatcher();

  // Literal keys on purpose: dynamically built i18n keys defeat the key-usage checks.
  const TIER_TEXT: Record<PasswordTier, { name: string; desc: string }> = {
    basic: { name: 'settings.passwordTier.basic.name', desc: 'settings.passwordTier.basic.desc' },
    standard: {
      name: 'settings.passwordTier.standard.name',
      desc: 'settings.passwordTier.standard.desc',
    },
    hardened: {
      name: 'settings.passwordTier.hardened.name',
      desc: 'settings.passwordTier.hardened.desc',
    },
    custom: {
      name: 'settings.passwordTier.custom.name',
      desc: 'settings.passwordTier.custom.desc',
    },
  };

  const INSTALL_COMMAND = './opentranscribe.sh download-models password-blocklist';

  let serverPolicy: PasswordPolicy | null = null;

  async function loadServerPolicy() {
    try {
      serverPolicy = await fetchPasswordPolicy();
    } catch {
      serverPolicy = null;
    }
  }

  onMount(loadServerPolicy);
  $: if (refreshKey) void loadServerPolicy();

  // An earlier install may hold `nist` / `stig`; show (and save) the tier it maps to.
  $: tier = normalizeTier(data.password_policy_profile) as PasswordTier;
  $: preset = isPreset(tier);
  $: editable = tier === 'custom' && !disabled;
  $: rules = effectiveRules(tier, data as PolicyValues);
  $: lengthConflict = data.password_max_length > 0 && data.password_max_length < data.password_min_length;

  // Without an installed list the check is skipped, so the preview must not promise it.
  $: listMissing = serverPolicy?.blocklist_status?.installed === false;
  $: ruleLines = buildRuleLines(rules, tier, data.password_hibp_enabled, listMissing);

  function compositionList(): string {
    const parts: string[] = [];
    if (rules.uppercase) parts.push($t('settings.localAuth.uppercase'));
    if (rules.lowercase) parts.push($t('settings.localAuth.lowercase'));
    if (rules.digit) parts.push($t('settings.localAuth.numbers'));
    if (rules.special) parts.push($t('settings.localAuth.specialChars'));
    return parts.join(', ');
  }

  function buildRuleLines(
    r: typeof rules,
    _tier: PasswordTier,
    online: boolean,
    missing: boolean
  ): string[] {
    const lines: string[] = [];
    lines.push(
      r.minLengthWithMfa !== r.minLength
        ? $t('settings.passwordTier.rule.minLengthMfa', {
            single: r.minLength,
            withMfa: r.minLengthWithMfa,
          })
        : $t('settings.passwordTier.rule.minLength', { min: r.minLength })
    );
    if (r.maxLength > 0) lines.push($t('settings.passwordTier.rule.maxLength', { max: r.maxLength }));
    const composition = r.uppercase || r.lowercase || r.digit || r.special;
    lines.push(
      composition
        ? $t('settings.passwordTier.rule.composition', { list: compositionList() })
        : $t('settings.passwordTier.rule.noComposition')
    );
    lines.push(
      r.maxAgeDays > 0
        ? $t('settings.passwordTier.rule.expiry', { days: r.maxAgeDays })
        : $t('settings.passwordTier.rule.noExpiry')
    );
    lines.push(
      r.historyCount > 0
        ? $t('settings.passwordTier.rule.history', { count: r.historyCount })
        : $t('settings.passwordTier.rule.noHistory')
    );
    lines.push(
      r.minAgeHours > 0
        ? $t('settings.passwordTier.rule.minAge', { hours: r.minAgeHours })
        : $t('settings.passwordTier.rule.noMinAge')
    );
    lines.push(
      r.blocklist && !missing
        ? $t('settings.passwordTier.rule.blocklistOn')
        : $t('settings.passwordTier.rule.blocklistOff')
    );
    if (online) lines.push($t('settings.passwordTier.rule.online'));
    return lines;
  }

  function selectTier(next: PasswordTier) {
    if (disabled || next === tier) return;
    data.password_policy_profile = next;
    dispatch('change');
  }

  function changed() {
    dispatch('change');
  }

  $: status = serverPolicy?.blocklist_status;
  $: statusKnown = status !== undefined;
</script>

<div class="policy-panel">
  <h3>{$t('settings.passwordTier.heading')}</h3>
  <p class="intro">{$t('settings.passwordTier.intro')}</p>

  <div
    class="tier-grid"
    role="radiogroup"
    aria-label={$t('settings.passwordTier.heading')}
    data-testid="password-tier-picker"
  >
    {#each PASSWORD_TIERS as option (option)}
      <label class="tier-card" class:selected={tier === option} class:inert={disabled}>
        <span class="tier-head">
          <input
            type="radio"
            name="password_policy_profile"
            value={option}
            checked={tier === option}
            {disabled}
            on:change={() => selectTier(option)}
          />
          <span class="tier-name">{$t(TIER_TEXT[option].name)}</span>
        </span>
        <span class="tier-desc">{$t(TIER_TEXT[option].desc)}</span>
      </label>
    {/each}
  </div>

  <p class="tradeoffs">{$t('settings.passwordTier.tradeoffs')}</p>

  <div class="policy-preview" data-testid="password-tier-rules">
    <strong>{$t('settings.passwordTier.effectiveHeading')}</strong>
    <ul>
      {#each ruleLines as line (line)}
        <li>{line}</li>
      {/each}
    </ul>
  </div>

  {#if preset}
    <p class="info-note">
      {$t('settings.passwordTier.fixedNote', { tier: $t(TIER_TEXT[tier].name) })}
    </p>
  {:else if tier === 'hardened'}
    <p class="info-note">{$t('settings.passwordTier.hardenedNote')}</p>
  {/if}

  {#if !preset}
  <fieldset class="values" disabled={!editable}>
    <div class="form-row">
      <FormField size="lg" id="password_min_length" label={$t('settings.localAuth.minPasswordLength')}>
        <input
          id="password_min_length"
          type="number"
          bind:value={data.password_min_length}
          on:input={changed}
          min="8"
          max="128"
        />
      </FormField>
      <FormField size="lg" id="password_max_length" label={$t('settings.passwordTier.maxLength')} hint={$t('settings.passwordTier.maxLengthHelp')} let:describedby>
        <input
          id="password_max_length" aria-describedby={describedby}
          type="number"
          bind:value={data.password_max_length}
          on:input={changed}
          min="0"
          max="1024"
        />
      </FormField>
    </div>
    {#if lengthConflict}
      <p class="field-error" role="alert">{$t('settings.passwordTier.lengthConflict')}</p>
    {/if}

    <div class="checkbox-grid">
      <label class="checkbox-label">
        <input type="checkbox" bind:checked={data.password_require_uppercase} on:change={changed} />
        <span>{$t('settings.localAuth.requireUppercase')}</span>
      </label>
      <label class="checkbox-label">
        <input type="checkbox" bind:checked={data.password_require_lowercase} on:change={changed} />
        <span>{$t('settings.localAuth.requireLowercase')}</span>
      </label>
      <label class="checkbox-label">
        <input type="checkbox" bind:checked={data.password_require_digit} on:change={changed} />
        <span>{$t('settings.localAuth.requireNumbers')}</span>
      </label>
      <label class="checkbox-label">
        <input type="checkbox" bind:checked={data.password_require_special} on:change={changed} />
        <span>{$t('settings.localAuth.requireSpecial')}</span>
      </label>
    </div>

    <div class="form-row">
      <FormField size="lg" id="password_max_age_days" label={$t('settings.localAuth.passwordExpiry')} hint={$t('settings.localAuth.passwordExpiryHelp')} let:describedby>
        <input
          id="password_max_age_days" aria-describedby={describedby}
          type="number"
          bind:value={data.password_max_age_days}
          on:input={changed}
          min="0"
          max="3650"
        />
      </FormField>
      <FormField size="lg" id="password_history_count" label={$t('settings.localAuth.passwordHistoryCount')} hint={$t('settings.localAuth.passwordHistoryHelp')} let:describedby>
        <input
          id="password_history_count" aria-describedby={describedby}
          type="number"
          bind:value={data.password_history_count}
          on:input={changed}
          min="0"
          max="100"
        />
      </FormField>
      <FormField size="lg" id="password_min_age_hours" label={$t('settings.passwordTier.minAge')} hint={$t('settings.passwordTier.minAgeHelp')} let:describedby>
        <input
          id="password_min_age_hours" aria-describedby={describedby}
          type="number"
          bind:value={data.password_min_age_hours}
          on:input={changed}
          min="0"
          max="8760"
        />
      </FormField>
    </div>
  </fieldset>
  {/if}

  <div class="screening">
    <FormField size="lg" id="password_blocklist_enabled" label={$t('settings.passwordTier.blocklistLabel')}>
      <select
        id="password_blocklist_enabled"
        bind:value={data.password_blocklist_enabled}
        on:change={changed}
        {disabled}
      >
        <option value="">{$t('settings.passwordTier.blocklistDefault')}</option>
        <option value="true">{$t('settings.passwordTier.blocklistOn')}</option>
        <option value="false">{$t('settings.passwordTier.blocklistOff')}</option>
      </select>
    </FormField>

    {#if statusKnown && status}
      <p
        class="list-status"
        class:missing={!status.installed}
        data-testid="blocklist-status"
        role="status"
      >
        {#if status.installed}
          {status.retrieved
            ? $t('settings.passwordTier.listInstalledDated', {
                entries: status.entries.toLocaleString(),
                date: status.retrieved,
              })
            : $t('settings.passwordTier.listInstalled', { entries: status.entries.toLocaleString() })}
          {#if status.source === 'custom'}
            {$t('settings.passwordTier.listCustom')}
          {/if}
        {:else}
          {$t('settings.passwordTier.listMissing')}
          <code>{INSTALL_COMMAND}</code>
        {/if}
      </p>
    {/if}

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={data.password_hibp_enabled}
        on:change={changed}
        {disabled}
      />
      <span>{$t('settings.passwordTier.onlineLabel')}</span>
    </label>
    <span class="help-text indented">{$t('settings.passwordTier.onlineHelp')}</span>
  </div>
</div>

<style>
  .policy-panel h3 {
    margin: 0 0 var(--space-2) 0;
  }

  .intro,
  .tradeoffs {
    margin: 0 0 var(--space-4) 0;
    font-size: var(--font-size-sm);
    line-height: 1.5;
    color: var(--color-text-secondary);
  }

  .tier-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: var(--space-3);
    margin-bottom: var(--space-4);
  }

  .tier-card {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    padding: var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    background: var(--color-bg);
    cursor: pointer;
  }

  .tier-card.selected {
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px var(--color-primary-alpha);
  }

  .tier-card.inert {
    cursor: not-allowed;
  }

  .tier-head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .tier-card input[type='radio'] {
    flex: none;
    width: 1rem;
    height: 1rem;
    margin: 0;
    cursor: inherit;
  }

  .tier-name {
    font-weight: var(--font-weight-semibold);
    font-size: var(--font-size-body);
    color: var(--color-text);
  }

  .tier-desc {
    font-size: var(--font-size-xs);
    line-height: 1.4;
    color: var(--color-text-secondary);
  }

  .policy-preview {
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
    padding: var(--space-3);
    margin-bottom: var(--space-4);
    font-size: var(--font-size-md);
  }

  .policy-preview ul {
    margin: var(--space-2) 0 0 0;
    padding-left: var(--space-5);
  }

  .info-note {
    margin: 0 0 var(--space-4) 0;
    padding: 0.6rem var(--space-3);
    border: 1px solid var(--color-info-border);
    border-radius: var(--radius-md);
    background: var(--color-info-bg);
    color: var(--color-text-secondary);
    font-size: var(--font-size-xs);
    line-height: 1.5;
  }

  .values {
    border: none;
    padding: 0;
    margin: 0 0 var(--space-4) 0;
  }

  .form-row {
    display: flex;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
  }

  .policy-panel :global(.form-field) {
    flex: 1;
    margin-bottom: var(--space-4);
  }

  .policy-panel :global(.form-field input[type='number']),
  .policy-panel :global(.form-field select) {
    width: 100%;
  }

  .policy-panel :global(.form-field input:disabled),
  .policy-panel :global(.form-field select:disabled) {
    cursor: not-allowed;
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

  .field-error {
    margin: calc(-1 * var(--space-2)) 0 var(--space-4) 0;
    font-size: var(--font-size-xs);
    color: var(--error-text);
  }

  .checkbox-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: var(--space-2);
    margin-bottom: var(--space-4);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    font-size: var(--font-size-md);
    margin-bottom: var(--space-2);
  }

  .checkbox-label input[type='checkbox'] {
    width: 1rem;
    height: 1rem;
    cursor: pointer;
  }

  .checkbox-label input:disabled {
    cursor: not-allowed;
  }

  .list-status {
    margin: 0 0 var(--space-4) 0;
    padding: 0.6rem var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    line-height: 1.5;
    color: var(--color-text-secondary);
  }

  .list-status.missing {
    border-color: rgba(245, 158, 11, 0.45);
    background: rgba(245, 158, 11, 0.12);
    color: var(--color-text);
  }

  .list-status code {
    display: block;
    margin-top: 0.35rem;
    font-size: var(--font-size-xs);
    user-select: all;
  }

  @media (max-width: 768px) {
    .form-row {
      flex-direction: column;
      gap: 0;
    }

    .checkbox-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
