<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import { getPrivateHosts, setPrivateHosts } from '$lib/api/mediaPrivateHosts';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let text = '';
  let savedText = '';
  let saving = false;

  onMount(async () => {
    try {
      text = savedText = (await getPrivateHosts()).join('\n');
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.mediaSources.privateHosts.loadFailed')));
    }
  });

  async function save() {
    saving = true;
    try {
      const hosts = text
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean);
      text = savedText = (await setPrivateHosts(hosts)).join('\n');
      toastStore.success($t('settings.mediaSources.privateHosts.saved'));
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.mediaSources.privateHosts.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<SettingsCard
  title={$t('settings.mediaSources.privateHosts.title')}
  description={$t('settings.mediaSources.privateHosts.desc')}
>
  <label class="field" for="private-hosts-list">
    <span>{$t('settings.mediaSources.privateHosts.label')}</span>
    <textarea
      id="private-hosts-list"
      class="form-input"
      dir="ltr"
      rows="4"
      spellcheck="false"
      bind:value={text}
    ></textarea>
    <small>{$t('settings.mediaSources.privateHosts.hint')}</small>
  </label>
  <div class="actions">
    <button
      type="button"
      class="btn btn-primary"
      on:click={save}
      disabled={saving || text === savedText}
    >
      {$t('settings.mediaSources.privateHosts.save')}
    </button>
  </div>
</SettingsCard>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--font-size-md);
    color: var(--text-color);
  }
  .field small {
    color: var(--text-muted);
  }
  .form-input {
    padding: var(--space-1-5) var(--space-2);
    font-family: monospace;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    margin-top: var(--space-3);
  }
</style>
