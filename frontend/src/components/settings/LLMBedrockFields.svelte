<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
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
    void (async () => {
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
            source: $t(SOURCE_KEYS[serverStatus.source ?? 'other'] ?? 'llm.bedrock.source.other')
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

  <div class="form-stack">
  <FormField id="bedrock-region" label={$t('llm.bedrock.region')} labelSize="base">
    <select id="bedrock-region" class="form-control" bind:value={region} {disabled} dir="ltr">
      <option value="">{$t('llm.bedrock.regionServerDefault')}</option>
      {#each regionOptions as option (option)}
        <option value={option}>{option}</option>
      {/each}
    </select>
  </FormField>

  <FormField id="bedrock-access-key-id" label={$t('llm.bedrock.accessKeyId')} labelSize="base">
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
  </FormField>

  <FormField id="bedrock-secret-access-key" label={$t('llm.bedrock.secretAccessKey')} labelSize="base">
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
  </FormField>

  <FormField id="bedrock-session-token" label={$t('llm.bedrock.sessionToken')} labelSize="base">
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
  </FormField>
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
    margin-bottom: var(--space-6);
    padding: var(--space-3) var(--space-4) var(--space-1);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--card-bg);
  }

  .bedrock-title-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-2);
  }

  .bedrock-title {
    margin: 0;
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .bedrock-status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin: 0 0 var(--space-4);
    font-size: var(--font-size-sm);
  }

  .status-chip {
    display: inline-block;
    padding: var(--space-0-5) var(--space-2);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-pill);
    color: var(--text-secondary);
    background: var(--surface-color);
  }

  .status-chip-ok {
    border-color: var(--success-color);
    color: var(--text-color);
  }

  .status-detail {
    color: var(--text-secondary);
  }



  .form-control {
    width: 100%;
  }

  .field-error {
    margin: var(--space-1-5) 0 0;
    font-size: var(--font-size-xs);
    color: var(--error-text);
  }

  .remove-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-4);
    font-size: var(--font-size-sm);
    color: var(--text-color);
    cursor: pointer;
  }
</style>
