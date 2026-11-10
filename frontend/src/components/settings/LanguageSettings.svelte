<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { locale, SUPPORTED_LANGUAGES, t } from "$stores/locale";
  import { toastStore } from "$stores/toast";
  import { isMachineTranslated } from "$lib/i18n/languages";

  // The <select> renders the store directly rather than mirroring it into a
  // second `bind:value` variable. The mirror was a reset-race: `bind:value` wrote
  // the user's pick while `$: selectedLanguage = $locale` wrote it back from the
  // store, so which one won depended on update ordering.
  function handleLanguageChange(event: Event) {
    const next = (event.currentTarget as HTMLSelectElement).value;
    if (next !== $locale) {
      locale.set(next);
      toastStore.success($t("settings.language.changed"));
    }
  }

  // Current language info; an unsupported locale previews the default (first) language.
  $: currentLanguage =
    SUPPORTED_LANGUAGES.find((l) => l.code === $locale) ?? SUPPORTED_LANGUAGES[0];
</script>

<div class="language-settings">
  <FormField id="ui-language" label={$t("settings.language.selectLabel")} labelSize="md" hint={$t("settings.language.hint")} let:describedby>
    <select
      id="ui-language" aria-describedby={describedby}
      class="form-select"
      value={$locale}
      on:change={handleLanguageChange}
    >
      {#each SUPPORTED_LANGUAGES as lang (lang.code)}
        <option value={lang.code}>
          {lang.nativeName} ({lang.name}){isMachineTranslated(lang.code)
            ? ` · ${$t("settings.language.machineTranslated")}`
            : ""}
        </option>
      {/each}
    </select>
  </FormField>

  <div class="language-preview">
    <div class="preview-item">
      <span class="preview-label">{$t("settings.language.selectLabel")}:</span>
      <span class="preview-value">
        {currentLanguage?.nativeName} ({currentLanguage?.name})
      </span>
    </div>
  </div>
</div>

<style>
  .language-settings {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }



  .form-select {
    transition:
      border-color var(--duration-fast),
      box-shadow var(--duration-fast);
    cursor: pointer;
    max-width: 300px;
    padding-right: var(--space-10);
  }


  .language-preview {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
  }

  .preview-item {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .preview-label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .preview-value {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }
</style>
