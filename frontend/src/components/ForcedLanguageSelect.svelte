<!--
  ForcedLanguageSelect.svelte — force the transcription language for one re-run (#1263).

  The first option keeps the account's saved language (which may itself be auto-detect);
  the rest are the languages the server accepts. `highlight` draws attention to the field
  when the file's detected language disagrees with its audio track's tag.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import { languageChoices } from '$lib/fileDetail/reprocessStages';

  /** `available_source_languages` from the system defaults: code -> display name. */
  export let languages: Record<string, string> | null | undefined = null;
  /** The chosen language code; null keeps the saved setting. Bindable. */
  export let value: string | null = null;
  /** The account's saved language code, named in the first option. */
  export let savedLanguage: string | null | undefined = null;
  /** Mark the field as the likely fix for a language mismatch. */
  export let highlight = false;
  export let id = 'reprocess-language-select';

  $: choices = languageChoices(languages);
  $: savedName = languages?.[savedLanguage ?? 'auto'] ?? languages?.auto ?? '';
</script>

{#if choices.length}
  <div
    class="setting-field forced-language"
    class:highlight
    data-testid="forced-language-select"
  >
    <label for={id}>{$t('reprocess.language')}</label>
    <select
      {id}
      class="language-select"
      value={value ?? ''}
      on:change={(e) => (value = e.currentTarget.value || null)}
    >
      <option value="">{$t('reprocess.languageDefault', { language: savedName })}</option>
      {#each choices as [code, name] (code)}
        <option value={code}>{name}</option>
      {/each}
    </select>
    <p class="language-hint">
      {highlight ? $t('reprocess.languageMismatchHint') : $t('reprocess.languageHint')}
    </p>
  </div>
{/if}

<style>
  .forced-language {
    margin-bottom: var(--space-4);
  }

  .forced-language.highlight {
    padding: var(--space-2-5) var(--space-3);
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
    border-radius: var(--radius-md);
  }

  .language-select {
    width: 100%;
  }

  .language-hint {
    margin: var(--space-1-5) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.4;
  }
</style>
