<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    createWatchSource,
    updateWatchSource,
    testWatchSource,
    browseDirectories,
    testMultipartRegex,
    type WatchSource,
    type SourceType,
    type Capabilities,
    type DirectoryListing,
  } from '$lib/api/watchSourcesApi';

  export let show = false;
  export let editingSource: WatchSource | null = null;
  export let capabilities: Capabilities = {
    watch_source_enabled: true,
    local_enabled: false,
    fs_events_enabled: false,
  };

  const dispatch = createEventDispatcher();

  type Tab = 'connection' | 'processing' | 'advanced' | 'organize';
  let activeTab: Tab = 'connection';
  let saving = false;
  let testing = false;
  let testResult: { success: boolean; message: string } | null = null;
  let lastEditingId: string | null = null;

  // Folder browser state (local sources)
  let browsing = false;
  let listing: DirectoryListing | null = null;

  // Multipart regex tester
  let regexSample = '';
  let regexResult: string | null = null;

  const DEFAULT_REGEX = '^(.+?)_P(\\d{3})(\\.[^.]+)$';

  function blankForm(): any {
    return {
      name: '',
      source_type: (capabilities.local_enabled ? 'local' : 's3') as SourceType,
      is_enabled: true,
      local_path: '',
      delete_after_import: false,
      s3_endpoint_url: '',
      s3_bucket_name: '',
      s3_prefix: '',
      s3_region: '',
      s3_access_key_id: '',
      s3_secret_key: '',
      s3_use_ssl: true,
      smb_server: '',
      smb_share: '',
      smb_path: '/',
      smb_username: '',
      smb_password: '',
      smb_domain: '',
      smb_port: 445,
      polling_interval_minutes: 15,
      use_fs_events: false,
      file_extensions: '',
      skip_files_older_than_days: 30,
      recursive: true,
      auto_transcribe: true,
      min_speakers: 1,
      max_speakers: 20,
      tag_names_csv: '',
      multipart_enabled: false,
      multipart_regex: DEFAULT_REGEX,
      multipart_time_window_hours: 24,
      multipart_wait_scans: 3,
      upload_stitched_to_source: false,
    };
  }

  let form: any = blankForm();

  function populate(src: WatchSource) {
    form = {
      ...blankForm(),
      name: src.name,
      source_type: src.source_type,
      is_enabled: src.is_enabled,
      local_path: src.local_path ?? '',
      delete_after_import: src.delete_after_import,
      s3_endpoint_url: src.s3_endpoint_url ?? '',
      s3_bucket_name: src.s3_bucket_name ?? '',
      s3_prefix: src.s3_prefix ?? '',
      s3_region: src.s3_region ?? '',
      s3_access_key_id: src.s3_access_key_id ?? '',
      s3_secret_key: '',
      s3_use_ssl: src.s3_use_ssl,
      smb_server: src.smb_server ?? '',
      smb_share: src.smb_share ?? '',
      smb_path: src.smb_path ?? '/',
      smb_username: src.smb_username ?? '',
      smb_password: '',
      smb_domain: src.smb_domain ?? '',
      smb_port: src.smb_port,
      polling_interval_minutes: src.polling_interval_minutes,
      use_fs_events: src.use_fs_events,
      file_extensions: src.file_extensions ?? '',
      skip_files_older_than_days: src.skip_files_older_than_days ?? null,
      recursive: src.recursive,
      auto_transcribe: src.auto_transcribe,
      min_speakers: src.min_speakers ?? 1,
      max_speakers: src.max_speakers ?? 20,
      tag_names_csv: (src.tag_names ?? []).join(', '),
      multipart_enabled: src.multipart_enabled,
      multipart_regex: src.multipart_regex || DEFAULT_REGEX,
      multipart_time_window_hours: src.multipart_time_window_hours,
      multipart_wait_scans: src.multipart_wait_scans,
      upload_stitched_to_source: src.upload_stitched_to_source,
    };
  }

  $: if (show) {
    if (editingSource && editingSource.uuid !== lastEditingId) {
      populate(editingSource);
      lastEditingId = editingSource.uuid;
      activeTab = 'connection';
      testResult = null;
    } else if (!editingSource && lastEditingId !== null) {
      form = blankForm();
      lastEditingId = null;
      activeTab = 'connection';
      testResult = null;
    }
  }

  $: isFormValid = (() => {
    if (!form.name?.trim()) return false;
    if (form.source_type === 's3') {
      return !!(
        form.s3_bucket_name?.trim() &&
        form.s3_access_key_id?.trim() &&
        (form.s3_secret_key?.trim() || editingSource?.has_s3_secret_key)
      );
    }
    if (form.source_type === 'smb') {
      return !!(form.smb_server?.trim() && form.smb_share?.trim());
    }
    return true; // local
  })();

  function buildPayload(): any {
    const tags = form.tag_names_csv
      .split(',')
      .map((s: string) => s.trim())
      .filter(Boolean);
    const payload: any = {
      name: form.name.trim(),
      is_enabled: form.is_enabled,
      polling_interval_minutes: Number(form.polling_interval_minutes),
      use_fs_events: form.use_fs_events,
      file_extensions: form.file_extensions?.trim() || null,
      skip_files_older_than_days:
        form.skip_files_older_than_days === null || form.skip_files_older_than_days === ''
          ? null
          : Number(form.skip_files_older_than_days),
      recursive: form.recursive,
      auto_transcribe: form.auto_transcribe,
      min_speakers: Number(form.min_speakers),
      max_speakers: Number(form.max_speakers),
      tag_names: tags,
      multipart_enabled: form.multipart_enabled,
      multipart_regex: form.multipart_regex,
      multipart_time_window_hours: Number(form.multipart_time_window_hours),
      multipart_wait_scans: Number(form.multipart_wait_scans),
      upload_stitched_to_source: form.upload_stitched_to_source,
    };
    if (form.source_type === 'local') {
      payload.local_path = form.local_path?.trim() || '';
      payload.delete_after_import = form.delete_after_import;
    } else if (form.source_type === 's3') {
      payload.s3_endpoint_url = form.s3_endpoint_url?.trim() || null;
      payload.s3_bucket_name = form.s3_bucket_name.trim();
      payload.s3_prefix = form.s3_prefix?.trim() || null;
      payload.s3_region = form.s3_region?.trim() || null;
      payload.s3_access_key_id = form.s3_access_key_id.trim();
      payload.s3_use_ssl = form.s3_use_ssl;
      if (form.s3_secret_key?.trim()) payload.s3_secret_key = form.s3_secret_key.trim();
    } else if (form.source_type === 'smb') {
      payload.smb_server = form.smb_server.trim();
      payload.smb_share = form.smb_share.trim();
      payload.smb_path = form.smb_path?.trim() || '/';
      payload.smb_username = form.smb_username?.trim() || null;
      payload.smb_domain = form.smb_domain?.trim() || null;
      payload.smb_port = Number(form.smb_port);
      if (form.smb_password?.trim()) payload.smb_password = form.smb_password.trim();
    }
    return payload;
  }

  async function handleSave() {
    if (!isFormValid) return;
    saving = true;
    try {
      if (editingSource) {
        const updated = await updateWatchSource(editingSource.uuid, buildPayload());
        toastStore.success($t('settings.watchSources.saved', { name: updated.name }));
        dispatch('saved', updated);
      } else {
        const payload = { ...buildPayload(), source_type: form.source_type };
        const created = await createWatchSource(payload);
        toastStore.success($t('settings.watchSources.saved', { name: created.name }));
        dispatch('saved', created);
      }
      handleClose();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.saveFailed')));
    } finally {
      saving = false;
    }
  }

  async function handleTest() {
    if (!editingSource) {
      toastStore.error($t('settings.watchSources.testSaveFirst'));
      return;
    }
    testing = true;
    testResult = null;
    try {
      const res = await testWatchSource(editingSource.uuid);
      testResult = { success: res.success, message: res.message };
    } catch (err) {
      testResult = { success: false, message: getErrorMessage(err, 'Test failed') };
    } finally {
      testing = false;
    }
  }

  async function openBrowser(path = '') {
    browsing = true;
    try {
      listing = await browseDirectories(path);
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.browseFailed')));
    } finally {
      browsing = false;
    }
  }

  function selectFolder(path: string) {
    form.local_path = path;
    listing = null;
  }

  async function runRegexTest() {
    if (!form.multipart_regex || !regexSample) return;
    try {
      const res = await testMultipartRegex(form.multipart_regex, regexSample);
      regexResult = res.matched
        ? $t('settings.watchSources.regexMatched', {
            base: res.base_name ?? '',
            part: res.part_number ?? '',
          })
        : res.error || $t('settings.watchSources.regexNoMatch');
    } catch (err) {
      regexResult = getErrorMessage(err, 'Test failed');
    }
  }

  function handleClose() {
    listing = null;
    regexResult = null;
    dispatch('close');
  }

  const TABS: { id: Tab; label: string }[] = [
    { id: 'connection', label: 'settings.watchSources.tabs.connection' },
    { id: 'processing', label: 'settings.watchSources.tabs.processing' },
    { id: 'advanced', label: 'settings.watchSources.tabs.advanced' },
    { id: 'organize', label: 'settings.watchSources.tabs.organize' },
  ];
</script>

<BaseModal isOpen={show} onClose={handleClose} maxWidth="640px">
  <svelte:fragment slot="header">
    <h2 class="modal-title">
      {editingSource
        ? $t('settings.watchSources.editTitle')
        : $t('settings.watchSources.addTitle')}
    </h2>
  </svelte:fragment>

  <div class="ws-form">
    <div class="form-group">
      <label for="ws-name">{$t('settings.watchSources.fields.name')}</label>
      <input
        id="ws-name"
        type="text"
        class="form-input"
        bind:value={form.name}
        placeholder={$t('settings.watchSources.fields.namePlaceholder')}
      />
    </div>

    <div class="ws-tabs" role="tablist">
      {#each TABS as tab}
        <button
          class="ws-tab"
          class:active={activeTab === tab.id}
          role="tab"
          aria-selected={activeTab === tab.id}
          on:click={() => (activeTab = tab.id)}
        >
          {$t(tab.label)}
        </button>
      {/each}
    </div>

    {#if activeTab === 'connection'}
      <div class="tab-panel">
        <div class="form-group">
          <label for="ws-type">{$t('settings.watchSources.fields.sourceType')}</label>
          <select
            id="ws-type"
            class="form-select"
            bind:value={form.source_type}
            disabled={!!editingSource}
          >
            {#if capabilities.local_enabled}
              <option value="local">{$t('settings.watchSources.types.local')}</option>
            {/if}
            <option value="s3">{$t('settings.watchSources.types.s3')}</option>
            <option value="smb">{$t('settings.watchSources.types.smb')}</option>
          </select>
          {#if editingSource}
            <p class="field-hint">{$t('settings.watchSources.typeLocked')}</p>
          {/if}
        </div>

        {#if form.source_type === 'local'}
          <div class="form-group">
            <label for="ws-localpath">{$t('settings.watchSources.fields.localPath')}</label>
            <div class="path-row">
              <input
                id="ws-localpath"
                type="text"
                class="form-input"
                bind:value={form.local_path}
                placeholder={$t('settings.watchSources.fields.localPathPlaceholder')}
              />
              <button class="btn btn-secondary" on:click={() => openBrowser(form.local_path)}>
                {$t('settings.watchSources.browse')}
              </button>
            </div>
            {#if browsing}
              <Spinner size="small" />
            {:else if listing}
              <div class="folder-browser">
                <div class="fb-header">
                  <span class="fb-current">/{listing.current_path}</span>
                  <button class="btn-link" on:click={() => selectFolder(listing?.current_path ?? '')}>
                    {$t('settings.watchSources.selectThisFolder')}
                  </button>
                </div>
                {#if listing.parent_path !== null && listing.parent_path !== undefined}
                  <button class="fb-entry" on:click={() => openBrowser(listing?.parent_path ?? '')}
                    >.. ({$t('settings.watchSources.parent')})</button
                  >
                {/if}
                {#each listing.directories as dir}
                  <button class="fb-entry" on:click={() => openBrowser(dir.path)}>📁 {dir.name}</button>
                {/each}
                {#if listing.directories.length === 0}
                  <div class="fb-empty">{$t('settings.watchSources.noSubfolders')}</div>
                {/if}
              </div>
            {/if}
          </div>
          <label class="checkbox-row">
            <input type="checkbox" bind:checked={form.delete_after_import} />
            <span>{$t('settings.watchSources.fields.deleteAfterImport')}</span>
          </label>
          {#if form.delete_after_import}
            <p class="warning-text">{$t('settings.watchSources.deleteWarning')}</p>
          {/if}
        {:else if form.source_type === 's3'}
          <div class="form-group">
            <label for="ws-s3ep">{$t('settings.watchSources.fields.s3Endpoint')}</label>
            <input id="ws-s3ep" type="text" class="form-input" bind:value={form.s3_endpoint_url} placeholder="https://s3.amazonaws.com (blank = AWS)" />
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="ws-s3b">{$t('settings.watchSources.fields.s3Bucket')}</label>
              <input id="ws-s3b" type="text" class="form-input" bind:value={form.s3_bucket_name} />
            </div>
            <div class="form-group">
              <label for="ws-s3p">{$t('settings.watchSources.fields.s3Prefix')}</label>
              <input id="ws-s3p" type="text" class="form-input" bind:value={form.s3_prefix} />
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="ws-s3r">{$t('settings.watchSources.fields.s3Region')}</label>
              <input id="ws-s3r" type="text" class="form-input" bind:value={form.s3_region} />
            </div>
            <div class="form-group">
              <label for="ws-s3ak">{$t('settings.watchSources.fields.s3AccessKey')}</label>
              <input id="ws-s3ak" type="text" class="form-input" bind:value={form.s3_access_key_id} />
            </div>
          </div>
          <div class="form-group">
            <label for="ws-s3sk">{$t('settings.watchSources.fields.s3SecretKey')}</label>
            <input
              id="ws-s3sk"
              type="password"
              class="form-input"
              bind:value={form.s3_secret_key}
              placeholder={editingSource?.has_s3_secret_key
                ? $t('settings.watchSources.secretStored')
                : ''}
            />
          </div>
          <label class="checkbox-row">
            <input type="checkbox" bind:checked={form.s3_use_ssl} />
            <span>{$t('settings.watchSources.fields.useSsl')}</span>
          </label>
        {:else if form.source_type === 'smb'}
          <div class="form-row">
            <div class="form-group">
              <label for="ws-smbs">{$t('settings.watchSources.fields.smbServer')}</label>
              <input id="ws-smbs" type="text" class="form-input" bind:value={form.smb_server} />
            </div>
            <div class="form-group">
              <label for="ws-smbsh">{$t('settings.watchSources.fields.smbShare')}</label>
              <input id="ws-smbsh" type="text" class="form-input" bind:value={form.smb_share} />
            </div>
          </div>
          <div class="form-group">
            <label for="ws-smbp">{$t('settings.watchSources.fields.smbPath')}</label>
            <input id="ws-smbp" type="text" class="form-input" bind:value={form.smb_path} />
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="ws-smbu">{$t('settings.watchSources.fields.smbUsername')}</label>
              <input id="ws-smbu" type="text" class="form-input" bind:value={form.smb_username} />
            </div>
            <div class="form-group">
              <label for="ws-smbpw">{$t('settings.watchSources.fields.smbPassword')}</label>
              <input
                id="ws-smbpw"
                type="password"
                class="form-input"
                bind:value={form.smb_password}
                placeholder={editingSource?.has_smb_password
                  ? $t('settings.watchSources.secretStored')
                  : ''}
              />
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="ws-smbd">{$t('settings.watchSources.fields.smbDomain')}</label>
              <input id="ws-smbd" type="text" class="form-input" bind:value={form.smb_domain} />
            </div>
            <div class="form-group">
              <label for="ws-smbport">{$t('settings.watchSources.fields.smbPort')}</label>
              <input id="ws-smbport" type="number" class="form-input" bind:value={form.smb_port} />
            </div>
          </div>
        {/if}
      </div>
    {/if}

    {#if activeTab === 'processing'}
      <div class="tab-panel">
        <div class="form-row">
          <div class="form-group">
            <label for="ws-interval">{$t('settings.watchSources.fields.pollingInterval')}</label>
            <input id="ws-interval" type="number" min="1" max="1440" class="form-input" bind:value={form.polling_interval_minutes} />
          </div>
          <div class="form-group">
            <label for="ws-age">{$t('settings.watchSources.fields.skipOlderThan')}</label>
            <input id="ws-age" type="number" min="0" class="form-input" bind:value={form.skip_files_older_than_days} />
          </div>
        </div>
        <div class="form-group">
          <label for="ws-ext">{$t('settings.watchSources.fields.fileExtensions')}</label>
          <input id="ws-ext" type="text" class="form-input" bind:value={form.file_extensions} placeholder=".mp4,.mp3,.wav (blank = all media)" />
        </div>
        <div class="form-row">
          <div class="form-group">
            <label for="ws-min">{$t('settings.watchSources.fields.minSpeakers')}</label>
            <input id="ws-min" type="number" min="1" class="form-input" bind:value={form.min_speakers} />
          </div>
          <div class="form-group">
            <label for="ws-max">{$t('settings.watchSources.fields.maxSpeakers')}</label>
            <input id="ws-max" type="number" min="1" class="form-input" bind:value={form.max_speakers} />
          </div>
        </div>
        <label class="checkbox-row">
          <input type="checkbox" bind:checked={form.recursive} />
          <span>{$t('settings.watchSources.fields.recursive')}</span>
        </label>
        <label class="checkbox-row">
          <input type="checkbox" bind:checked={form.auto_transcribe} />
          <span>{$t('settings.watchSources.fields.autoTranscribe')}</span>
        </label>
        {#if form.source_type === 'local' && capabilities.fs_events_enabled}
          <label class="checkbox-row">
            <input type="checkbox" bind:checked={form.use_fs_events} />
            <span>{$t('settings.watchSources.fields.useFsEvents')}</span>
          </label>
        {/if}
      </div>
    {/if}

    {#if activeTab === 'advanced'}
      <div class="tab-panel">
        <label class="checkbox-row">
          <input type="checkbox" bind:checked={form.multipart_enabled} />
          <span>{$t('settings.watchSources.fields.multipartEnabled')}</span>
        </label>
        {#if form.multipart_enabled}
          <div class="form-group">
            <label for="ws-regex">{$t('settings.watchSources.fields.multipartRegex')}</label>
            <input id="ws-regex" type="text" class="form-input" bind:value={form.multipart_regex} />
          </div>
          <div class="form-group">
            <label for="ws-regextest">{$t('settings.watchSources.regexTester')}</label>
            <div class="path-row">
              <input id="ws-regextest" type="text" class="form-input" bind:value={regexSample} placeholder="meeting_P001.mp4" />
              <button class="btn btn-secondary" on:click={runRegexTest}>{$t('settings.watchSources.testRegex')}</button>
            </div>
            {#if regexResult}<p class="field-hint">{regexResult}</p>{/if}
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="ws-mpw">{$t('settings.watchSources.fields.multipartWindow')}</label>
              <input id="ws-mpw" type="number" min="1" class="form-input" bind:value={form.multipart_time_window_hours} />
            </div>
            <div class="form-group">
              <label for="ws-mpws">{$t('settings.watchSources.fields.multipartWaitScans')}</label>
              <input id="ws-mpws" type="number" min="1" class="form-input" bind:value={form.multipart_wait_scans} />
            </div>
          </div>
          {#if form.source_type !== 'local'}
            <label class="checkbox-row">
              <input type="checkbox" bind:checked={form.upload_stitched_to_source} />
              <span>{$t('settings.watchSources.fields.uploadStitched')}</span>
            </label>
          {/if}
        {/if}
      </div>
    {/if}

    {#if activeTab === 'organize'}
      <div class="tab-panel">
        <div class="form-group">
          <label for="ws-tags">{$t('settings.watchSources.fields.tags')}</label>
          <input id="ws-tags" type="text" class="form-input" bind:value={form.tag_names_csv} placeholder="meetings, auto-import" />
          <p class="field-hint">{$t('settings.watchSources.fields.tagsHint')}</p>
        </div>
        <label class="checkbox-row">
          <input type="checkbox" bind:checked={form.is_enabled} />
          <span>{$t('settings.watchSources.fields.enabled')}</span>
        </label>
      </div>
    {/if}

    <div role="status" aria-live="polite">
      {#if testResult}
        <div class="test-result" class:success={testResult.success} class:failure={!testResult.success}>
          {testResult.success ? '✓' : '✗'} {testResult.message}
        </div>
      {/if}
    </div>
  </div>

  <svelte:fragment slot="footer">
    <div class="footer-row">
      <button
        class="btn btn-secondary"
        on:click={handleTest}
        disabled={testing || !editingSource}
        title={!editingSource ? $t('settings.watchSources.testSaveFirst') : ''}
      >
        {#if testing}<Spinner size="small" />{:else}{$t('settings.watchSources.testConnection')}{/if}
      </button>
      <div class="footer-actions">
        <button class="btn btn-secondary" on:click={handleClose}>{$t('common.cancel')}</button>
        <button class="btn btn-primary" on:click={handleSave} disabled={saving || !isFormValid}>
          {saving ? $t('common.saving') : editingSource ? $t('common.update') : $t('common.save')}
        </button>
      </div>
    </div>
  </svelte:fragment>
</BaseModal>

<style>
  .ws-form {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .modal-title {
    margin: 0;
    font-size: 1.1rem;
  }
  .ws-tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border-color);
    flex-wrap: wrap;
  }
  .ws-tab {
    background: none;
    border: none;
    padding: 8px 12px;
    cursor: pointer;
    color: var(--text-secondary);
    border-bottom: 2px solid transparent;
    font-size: 0.9rem;
  }
  .ws-tab.active {
    color: var(--primary-color);
    border-bottom-color: var(--primary-color);
    font-weight: 600;
  }
  .tab-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .form-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
  }
  .form-row {
    display: flex;
    gap: 12px;
  }
  .form-group label {
    font-size: 0.85rem;
    color: var(--text-secondary);
  }
  .form-input,
  .form-select {
    padding: 8px 10px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 0.9rem;
  }
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.9rem;
    cursor: pointer;
  }
  .path-row {
    display: flex;
    gap: 8px;
  }
  .path-row .form-input {
    flex: 1;
  }
  .field-hint {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin: 2px 0 0;
  }
  .warning-text {
    font-size: 0.8rem;
    color: var(--warning-color);
    margin: 0;
  }
  .folder-browser {
    border: 1px solid var(--border-color);
    border-radius: 6px;
    max-height: 200px;
    overflow-y: auto;
    margin-top: 6px;
  }
  .fb-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 10px;
    border-bottom: 1px solid var(--border-color);
    background: var(--button-hover);
  }
  .fb-current {
    font-family: monospace;
    font-size: 0.8rem;
  }
  .fb-entry {
    display: block;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    padding: 6px 10px;
    cursor: pointer;
    color: var(--text-color);
    font-size: 0.85rem;
  }
  .fb-entry:hover {
    background: var(--button-hover);
  }
  .fb-empty {
    padding: 8px 10px;
    color: var(--text-secondary);
    font-size: 0.8rem;
  }
  .btn-link {
    background: none;
    border: none;
    color: var(--primary-color);
    cursor: pointer;
    font-size: 0.8rem;
  }
  .test-result {
    padding: 8px 10px;
    border-radius: 6px;
    font-size: 0.85rem;
  }
  .test-result.success {
    background: rgba(34, 197, 94, 0.12);
    color: var(--success-color, #16a34a);
  }
  .test-result.failure {
    background: rgba(239, 68, 68, 0.12);
    color: var(--error-color, #dc2626);
  }
  .footer-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
  }
  .footer-actions {
    display: flex;
    gap: 8px;
  }
</style>
