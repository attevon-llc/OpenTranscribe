<script lang="ts">
  /**
   * The Amazon Bedrock account fields of an LLM configuration: region and (optionally) the
   * configuration's own AWS access key.
   *
   * The three credential values are WRITE-ONLY. They are never pre-filled (the server only
   * sends `has_aws_credentials`), so a stored key shows as "saved, leave blank to keep it" and
   * a blank field on save means "keep". Leaving them all blank means the server's own AWS
   * credentials (an IAM role, profile or environment) are used, which is the preferred setup
   * on AWS. An administrator also sees whether the server's credentials currently resolve.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import {
    LLMSettingsApi,
    type BedrockServerCredentialStatus
  } from '$lib/api/llmSettings';
  import InfoTip from '$components/settings/transcription/InfoTip.svelte';

  export let region = '';
  export let accessKeyId = '';
  export let secretAccessKey = '';
  export let sessionToken = '';
  export let clearCredentials = false;
  /** The configuration being edited already stores its own key pair. */
  export let hasSavedCredentials = false;
  export let regions: string[] = [];
  export let models: string[] = [];
  export let isAdmin = false;
  export let disabled = false;

  export const modelListId = 'bedrock-model-suggestions';

  let serverStatus: BedrockServerCredentialStatus | null = null;

  onMount(() => {
    if (!isAdmin) return;
    (async () => {
      try {
        serverStatus = await LLMSettingsApi.getBedrockServerCredentials();
      } catch {
        // Informational only: the form works without it.
        serverStatus = null;
      }
    })();
  });

  const SOURCE_KEYS: Record<string, string> = {
    environment: 'llm.bedrock.source.environment',
    shared_profile: 'llm.bedrock.source.sharedProfile',
    assumed_role: 'llm.bedrock.source.assumedRole',
    instance_role: 'llm.bedrock.source.instanceRole',
    container_role: 'llm.bedrock.source.containerRole',
    other: 'llm.bedrock.source.other'
  };

  $: entering = Boolean(accessKeyId.trim() || secretAccessKey.trim());
  $: usingSaved = hasSavedCredentials && !clearCredentials && !entering;
  $: usingServer = !usingSaved && !entering;
  $: pairIncomplete = Boolean(accessKeyId.trim()) !== Boolean(secretAccessKey.trim());
  // A region saved earlier may be outside the suggestion list; keep it selectable.
  $: regionOptions = region && !regions.includes(region) ? [region, ...regions] : regions;
  $: serverLine =
    isAdmin && usingServer && serverStatus
      ? serverStatus.found
        ? $t('llm.bedrock.statusServerFound', {
            source: $t(SOURCE_KEYS[serverStatus.source ?? 'other'] ?? SOURCE_KEYS.other)
          })
        : $t('llm.bedrock.statusServerMissing')
      : '';
</script>

<div class="bedrock-block">
  <div class="bedrock-title-row">
    <h4 class="bedrock-title">{$t('llm.bedrock.title')}</h4>
    <InfoTip inline label={$t('llm.bedrock.infoLabel')}>
      <p>{$t('llm.bedrock.info')}</p>
      {#if isAdmin}
        <p>{$t('llm.bedrock.infoAdmin')}</p>
      {/if}
    </InfoTip>
  </div>

  <p class="bedrock-status" role="status" data-testid="bedrock-credential-status">
    <span
      class="status-chip"
      class:status-chip-ok={usingSaved || (usingServer && serverStatus?.found)}
    >
      {#if usingSaved}
        {$t('llm.bedrock.statusSaved')}
      {:else if usingServer}
        {$t('llm.bedrock.statusServer')}
      {:else}
        {$t('llm.bedrock.statusEntered')}
      {/if}
    </span>
    {#if serverLine}
      <span class="status-detail">{serverLine}</span>
    {/if}
  </p>

  <div class="form-group">
    <label for="bedrock-region">{$t('llm.bedrock.region')}</label>
    <select id="bedrock-region" class="form-control" bind:value={region} {disabled} dir="ltr">
      <option value="">{$t('llm.bedrock.regionServerDefault')}</option>
      {#each regionOptions as option (option)}
        <option value={option}>{option}</option>
      {/each}
    </select>
  </div>

  <div class="form-group">
    <label for="bedrock-access-key-id">{$t('llm.bedrock.accessKeyId')}</label>
    <input
      type="text"
      id="bedrock-access-key-id"
      class="form-control"
      bind:value={accessKeyId}
      {disabled}
      autocomplete="off"
      autocapitalize="off"
      spellcheck="false"
      dir="ltr"
      placeholder={hasSavedCredentials ? $t('llm.bedrock.savedHint') : ''}
    />
  </div>

  <div class="form-group">
    <label for="bedrock-secret-access-key">{$t('llm.bedrock.secretAccessKey')}</label>
    <input
      type="password"
      id="bedrock-secret-access-key"
      class="form-control"
      bind:value={secretAccessKey}
      {disabled}
      autocomplete="new-password"
      spellcheck="false"
      dir="ltr"
      placeholder={hasSavedCredentials ? $t('llm.bedrock.savedHint') : ''}
    />
    {#if pairIncomplete}
      <p class="field-error" role="alert">{$t('llm.bedrock.pairRequired')}</p>
    {/if}
  </div>

  <div class="form-group">
    <label for="bedrock-session-token">{$t('llm.bedrock.sessionToken')}</label>
    <input
      type="password"
      id="bedrock-session-token"
      class="form-control"
      bind:value={sessionToken}
      {disabled}
      autocomplete="new-password"
      spellcheck="false"
      dir="ltr"
    />
  </div>

  {#if hasSavedCredentials}
    <label class="remove-row" for="bedrock-clear-credentials">
      <input
        type="checkbox"
        id="bedrock-clear-credentials"
        bind:checked={clearCredentials}
        disabled={disabled || entering}
      />
      <span>{$t('llm.bedrock.removeCredentials')}</span>
    </label>
  {/if}

  <datalist id={modelListId}>
    {#each models as suggestion (suggestion)}
      <option value={suggestion}></option>
    {/each}
  </datalist>
</div>

<style>
  .bedrock-block {
    margin-bottom: 1.5rem;
    padding: 0.75rem 1rem 0.25rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--card-bg);
  }

  .bedrock-title-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
  }

  .bedrock-title {
    margin: 0;
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .bedrock-status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    margin: 0 0 1rem;
    font-size: 0.8125rem;
  }

  .status-chip {
    display: inline-block;
    padding: 0.125rem 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    color: var(--text-muted);
    background: var(--surface-color, transparent);
  }

  .status-chip-ok {
    border-color: var(--success-color);
    color: var(--text-color);
  }

  .status-detail {
    color: var(--text-muted);
  }

  .form-group {
    margin-bottom: 1rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.5rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .form-control {
    width: 100%;
  }

  .field-error {
    margin: 0.375rem 0 0;
    font-size: 0.75rem;
    color: var(--error-color);
  }

  .remove-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 1rem;
    font-size: 0.8125rem;
    color: var(--text-color);
    cursor: pointer;
  }
</style>
