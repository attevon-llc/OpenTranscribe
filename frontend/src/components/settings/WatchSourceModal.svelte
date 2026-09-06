<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import MultiSelect from 'svelte-multiselect';
  import axiosInstance from '$lib/axios';
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
  import { listTags } from '$lib/api/tags';

  export let show = false;
  export let editingSource: WatchSource | null = null;
  export let capabilities: Capabilities = {
    watch_source_enabled: true,
    local_enabled: false,
    fs_events_enabled: false,
    fs_events_mode: 'auto',
  };

  const dispatch = createEventDispatcher();

  const DEFAULT_REGEX = '^(.+?)_P(\\d{3})(\\.[^.]+)$';

  // ── Stepper state ──
  let currentStepIndex = 0;
  let maxStepReached = 0;
  $: steps = [
    { id: 'connection', label: $t('settings.watchSources.tabs.connection') },
    { id: 'processing', label: $t('settings.watchSources.tabs.processing') },
    { id: 'advanced', label: $t('settings.watchSources.tabs.advanced') },
    { id: 'organize', label: $t('settings.watchSources.tabs.organize') },
  ];
  $: currentStep = steps[currentStepIndex];
  $: isFirstStep = currentStepIndex === 0;
  $: isLastStep = currentStepIndex === steps.length - 1;
  $: if (currentStepIndex > maxStepReached) maxStepReached = currentStepIndex;

  let saving = false;
  let testing = false;
  let testResult: { success: boolean; message: string } | null = null;

  // Folder browser (local)
  let browsing = false;
  let listing: DirectoryListing | null = null;

  // Multipart regex tester
  let regexSample = '';
  let regexResult: string | null = null;

  // Tags + collections (Organize step) — compact svelte-multiselect.
  // Tags are plain names (the backend creates tags on import). Collections are
  // {label,value} options where value is the collection UUID; brand-new names
  // typed by the user are created via the API at save time.
  type ColOption = { label: string; value: string };
  let selectedTags: string[] = [];
  let selectedCollections: ColOption[] = [];
  let tagNames: string[] = [];
  let colOptions: ColOption[] = [];
  let availableCollections: Array<{ uuid: string; name: string }> = [];

  function blankForm(): Record<string, unknown> {
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
      multipart_enabled: false,
      multipart_regex: DEFAULT_REGEX,
      multipart_time_window_hours: 24,
      multipart_wait_scans: 3,
      upload_stitched_to_source: false,
    };
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let form: any = blankForm();

  onMount(() => {
    (async () => {
      try {
        const [colRes, tags] = await Promise.all([axiosInstance.get('/collections'), listTags()]);
        availableCollections = colRes.data ?? [];
        colOptions = availableCollections.map((c) => ({ label: c.name, value: c.uuid }));
        tagNames = (tags ?? []).map((t) => t.name);
        mapEditingCollections();
      } catch {
        // Non-fatal: organize step still works with create-new.
      }
    })();
  });

  function mapEditingCollections() {
    if (editingSource?.collection_ids?.length && availableCollections.length) {
      selectedCollections = editingSource.collection_ids
        .map((id) => availableCollections.find((c) => c.uuid === id))
        .filter((c): c is { uuid: string; name: string } => !!c)
        .map((c) => ({ label: c.name, value: c.uuid }));
    }
  }

  /** Resolve selected collections to UUIDs, creating any brand-new names. */
  async function resolveCollectionIds(): Promise<string[]> {
    const ids: string[] = [];
    for (const opt of selectedCollections) {
      const known = availableCollections.find((c) => c.uuid === opt.value);
      if (known) {
        ids.push(known.uuid);
        continue;
      }
      // A newly-typed name (value === label, not an existing uuid): create it.
      try {
        const { data } = await axiosInstance.post('/collections', { name: opt.label });
        availableCollections = [...availableCollections, { uuid: data.uuid, name: data.name }];
        colOptions = [...colOptions, { label: data.name, value: data.uuid }];
        ids.push(data.uuid);
      } catch {
        // Skip a collection that couldn't be created; don't block the save.
        toastStore.error(getErrorMessage(null, $t('settings.watchSources.collectionCreateFailed')));
      }
    }
    return ids;
  }

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
      multipart_enabled: src.multipart_enabled,
      multipart_regex: src.multipart_regex || DEFAULT_REGEX,
      multipart_time_window_hours: src.multipart_time_window_hours,
      multipart_wait_scans: src.multipart_wait_scans,
      upload_stitched_to_source: src.upload_stitched_to_source,
    };
    selectedTags = [...(src.tag_names ?? [])];
    mapEditingCollections();
  }

  // Initialize the form when the modal transitions to open, so blankForm() reads
  // the real `capabilities` prop (which arrives after init) — otherwise new
  // sources would wrongly default to S3 even when a local folder is available.
  let prevShow = false;
  $: if (show && !prevShow) {
    prevShow = true;
    currentStepIndex = 0;
    testResult = null;
    listing = null;
    regexResult = null;
    if (editingSource) {
      populate(editingSource);
      maxStepReached = steps.length - 1; // edits can jump to any step
    } else {
      form = blankForm();
      selectedTags = [];
      selectedCollections = [];
      maxStepReached = 0;
    }
  }
  $: if (!show && prevShow) prevShow = false;

  $: connectionValid = (() => {
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

  $: canProceed = currentStep?.id === 'connection' ? connectionValid : true;
  $: canSave = connectionValid;

  function goNext() {
    if (!isLastStep && canProceed) currentStepIndex += 1;
  }
  function goBack() {
    if (!isFirstStep) currentStepIndex -= 1;
  }
  function goToStep(i: number) {
    if (i <= maxStepReached) currentStepIndex = i;
  }

  function buildPayload(collectionIds: string[]): Record<string, unknown> {
    const payload: Record<string, unknown> = {
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
      tag_names: selectedTags,
      collection_ids: collectionIds,
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
    if (!canSave) return;
    saving = true;
    try {
      const collectionIds = await resolveCollectionIds();
      if (editingSource) {
        const updated = await updateWatchSource(
          editingSource.uuid,
          buildPayload(collectionIds) as Parameters<typeof updateWatchSource>[1]
        );
        toastStore.success($t('settings.watchSources.saved', { name: updated.name }));
        dispatch('saved', updated);
      } else {
        const created = await createWatchSource({
          ...buildPayload(collectionIds),
          source_type: form.source_type,
        } as unknown as Parameters<typeof createWatchSource>[0]);
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
      testResult = { success: false, message: getErrorMessage(err, $t('settings.watchSources.testFailed')) };
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
      regexResult = getErrorMessage(err, $t('settings.watchSources.testFailed'));
    }
  }

  function handleClose() {
    listing = null;
    regexResult = null;
    dispatch('close');
  }
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

    <!-- Stepper indicator -->
    <div class="step-indicator" role="navigation" aria-label={$t('settings.watchSources.title')}>
      {#each steps as step, i (step.id)}
        <button
          type="button"
          class="step-item"
          class:active={currentStepIndex === i}
          class:completed={i < currentStepIndex}
          class:visited={i > currentStepIndex && i <= maxStepReached}
          on:click={() => goToStep(i)}
          disabled={i > maxStepReached}
          aria-current={currentStepIndex === i ? 'step' : undefined}
        >
          <span class="step-dot">
            {#if i < currentStepIndex}
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
            {:else}
              <span class="step-number">{i + 1}</span>
            {/if}
          </span>
          <span class="step-label">{step.label}</span>
        </button>
        {#if i < steps.length - 1}
          <div class="step-line" class:completed={i < currentStepIndex}></div>
        {/if}
      {/each}
    </div>

    <!-- Step: Connection -->
    {#if currentStep?.id === 'connection'}
      <div class="step-panel">
        <div class="form-group">
          <label for="ws-type">{$t('settings.watchSources.fields.sourceType')}</label>
          <select id="ws-type" class="form-select" bind:value={form.source_type} disabled={!!editingSource}>
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
              <input id="ws-localpath" type="text" class="form-input" bind:value={form.local_path} placeholder={$t('settings.watchSources.fields.localPathPlaceholder')} />
              <button type="button" class="btn btn-secondary" on:click={() => openBrowser(form.local_path)}>{$t('settings.watchSources.browse')}</button>
            </div>
            {#if browsing}
              <Spinner size="small" />
            {:else if listing}
              <div class="folder-browser">
                <div class="fb-header">
                  <span class="fb-current">/{listing.current_path}</span>
                  <button type="button" class="btn-link" on:click={() => selectFolder(listing?.current_path ?? '')}>{$t('settings.watchSources.selectThisFolder')}</button>
                </div>
                {#if listing.parent_path !== null && listing.parent_path !== undefined}
                  <button type="button" class="fb-entry" on:click={() => openBrowser(listing?.parent_path ?? '')}>.. ({$t('settings.watchSources.parent')})</button>
                {/if}
                {#each listing.directories as dir}
                  <button type="button" class="fb-entry" on:click={() => openBrowser(dir.path)}>
                    <svg class="fb-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                    </svg>
                    {dir.name}
                  </button>
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
            <input id="ws-s3sk" type="password" class="form-input" bind:value={form.s3_secret_key} placeholder={editingSource?.has_s3_secret_key ? $t('settings.watchSources.secretStored') : ''} />
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
              <input id="ws-smbpw" type="password" class="form-input" bind:value={form.smb_password} placeholder={editingSource?.has_smb_password ? $t('settings.watchSources.secretStored') : ''} />
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

    <!-- Step: Processing -->
    {#if currentStep?.id === 'processing'}
      <div class="step-panel">
        <div class="form-row">
          <div class="form-group">
            <label for="ws-interval">{$t('settings.watchSources.fields.pollingInterval')}</label>
            <input id="ws-interval" type="number" min="1" max="1440" class="form-input" bind:value={form.polling_interval_minutes} />
          </div>
          <div class="form-group">
            <label for="ws-age">{$t('settings.watchSources.fields.skipOlderThan')}</label>
            <input id="ws-age" type="number" min="0" class="form-input" bind:value={form.skip_files_older_than_days} />
            <p class="field-hint">{$t('settings.watchSources.skipOlderHelp')}</p>
          </div>
        </div>
        <div class="form-group">
          <label for="ws-ext">{$t('settings.watchSources.fields.fileExtensions')}</label>
          <input id="ws-ext" type="text" class="form-input" bind:value={form.file_extensions} placeholder=".mp4,.mp3,.wav" />
          <p class="field-hint">{$t('settings.watchSources.extensionsHelp')}</p>
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

    <!-- Step: Advanced (multi-part stitching) -->
    {#if currentStep?.id === 'advanced'}
      <div class="step-panel">
        <p class="step-hint">{$t('settings.watchSources.stitchHelp')}</p>
        <label class="checkbox-row">
          <input type="checkbox" bind:checked={form.multipart_enabled} />
          <span>{$t('settings.watchSources.fields.multipartEnabled')}</span>
        </label>
        {#if form.multipart_enabled}
          <div class="form-group">
            <label for="ws-regex">{$t('settings.watchSources.fields.multipartRegex')}</label>
            <input id="ws-regex" type="text" class="form-input" bind:value={form.multipart_regex} />
            <p class="field-hint">{$t('settings.watchSources.regexHelp')}</p>
          </div>
          <div class="form-group">
            <label for="ws-regextest">{$t('settings.watchSources.regexTester')}</label>
            <div class="path-row">
              <input id="ws-regextest" type="text" class="form-input" bind:value={regexSample} placeholder="meeting_P001.mp4" />
              <button type="button" class="btn btn-secondary" on:click={runRegexTest}>{$t('settings.watchSources.testRegex')}</button>
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

    <!-- Step: Organize (tags + collections) -->
    {#if currentStep?.id === 'organize'}
      <div class="step-panel">
        <p class="step-hint">{$t('settings.watchSources.organizeHelp')}</p>
        <div class="form-group">
          <label for="ws-tags">{$t('settings.watchSources.fields.tags')}</label>
          <div class="ms-wrap">
            <MultiSelect
              --sms-options-bg="var(--surface-color)"
              id="ws-tags"
              options={tagNames}
              bind:selected={selectedTags}
              allowUserOptions="append"
              placeholder={$t('settings.watchSources.tagsPlaceholder')}
            />
          </div>
        </div>
        <div class="form-group">
          <label for="ws-cols">{$t('settings.watchSources.collectionsLabel')}</label>
          <div class="ms-wrap">
            <MultiSelect
              --sms-options-bg="var(--surface-color)"
              id="ws-cols"
              options={colOptions}
              bind:selected={selectedCollections}
              allowUserOptions="append"
              placeholder={$t('settings.watchSources.collectionsPlaceholder')}
            />
          </div>
        </div>
        <label class="checkbox-row enabled-row">
          <input type="checkbox" bind:checked={form.is_enabled} />
          <span>{$t('settings.watchSources.fields.enabled')}</span>
        </label>
      </div>
    {/if}

    <div role="status" aria-live="polite">
      {#if testResult}
        <div class="test-result" class:success={testResult.success} class:failure={!testResult.success}>
          {#if testResult.success}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12" /></svg>
          {:else}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          {/if}
          <span>{testResult.message}</span>
        </div>
      {/if}
    </div>
  </div>

  <svelte:fragment slot="footer">
    <div class="footer-row">
      <div class="footer-left">
        {#if currentStep?.id === 'connection'}
          <button class="btn btn-secondary" on:click={handleTest} disabled={testing || !editingSource} title={!editingSource ? $t('settings.watchSources.testSaveFirst') : ''}>
            {#if testing}<Spinner size="small" />{:else}{$t('settings.watchSources.testConnection')}{/if}
          </button>
        {/if}
      </div>
      <div class="footer-actions">
        <button class="btn btn-secondary" on:click={handleClose}>{$t('common.cancel')}</button>
        {#if !isFirstStep}
          <button class="btn btn-secondary" on:click={goBack}>{$t('common.back')}</button>
        {/if}
        {#if !isLastStep}
          <button class="btn btn-primary" on:click={goNext} disabled={!canProceed}>{$t('common.next')}</button>
        {/if}
        {#if isLastStep || editingSource}
          <button class="btn btn-primary" on:click={handleSave} disabled={saving || !canSave}>
            {saving ? $t('common.saving') : editingSource ? $t('common.update') : $t('common.save')}
          </button>
        {/if}
      </div>
    </div>
  </svelte:fragment>
</BaseModal>

<style>
  .ws-form {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .modal-title {
    margin: 0;
    font-size: 1.1rem;
  }

  /* Stepper indicator */
  .step-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 4px 0 8px;
  }
  .step-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 6px;
    background: transparent;
    border: none;
    border-radius: 6px;
    box-shadow: none;
    cursor: pointer;
    flex-shrink: 0;
  }
  .step-item:disabled {
    cursor: default;
  }
  .step-item:not(:disabled):hover {
    background: rgba(var(--primary-color-rgb), 0.08);
    transform: none;
    box-shadow: none;
  }
  .step-dot {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 0.7rem;
    font-weight: 600;
    border: 2px solid var(--border-color);
    background: var(--surface-color);
    color: var(--text-secondary);
    flex-shrink: 0;
    transition: all 0.2s ease;
  }
  .step-item.active .step-dot,
  .step-item.completed .step-dot {
    border-color: var(--primary-color);
    background: var(--primary-color);
    color: white;
  }
  .step-item.visited .step-dot {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
  }
  .step-label {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-secondary);
    white-space: nowrap;
  }
  .step-item.active .step-label {
    color: var(--primary-on-surface);
    font-weight: 600;
  }
  .step-item.completed .step-label,
  .step-item.visited .step-label {
    color: var(--primary-on-surface);
  }
  .step-line {
    flex: 1;
    max-width: 40px;
    height: 2px;
    background: var(--border-color);
    margin: 0 4px;
  }
  .step-line.completed {
    background: var(--primary-color);
  }

  .step-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: 180px;
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
  /* .form-input / .form-select inherit the app's global input/select styling. */
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.9rem;
    cursor: pointer;
  }
  /* Override the global `input { width:100% }` base so checkboxes stay square. */
  .checkbox-row input[type='checkbox'] {
    width: 16px;
    height: 16px;
    min-height: 0;
    margin: 0;
    padding: 0;
    flex: none;
    cursor: pointer;
    accent-color: var(--primary-color);
  }
  .enabled-row {
    margin-top: 4px;
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
  .step-hint {
    font-size: 0.85rem;
    color: var(--text-secondary);
    margin: 0;
    line-height: 1.5;
  }
  /* Theme svelte-multiselect to match the app's inputs (light/dark) and
     neutralize the aggressive global button styling on its internal buttons. */
  .ms-wrap :global(.multiselect) {
    --sms-bg: var(--input-background);
    --sms-border: 1px solid var(--input-border);
    --sms-focus-border: 1px solid var(--primary-color);
    --sms-text-color: var(--text-color);
    --sms-placeholder-color: var(--text-secondary);
    --sms-options-bg: var(--surface-color);
    --sms-li-active-bg: var(--button-hover);
    --sms-selected-bg: rgba(var(--primary-color-rgb), 0.15);
    --sms-selected-text-color: var(--text-color);
    border-radius: 4px;
    min-height: 40px;
    font-size: 0.9rem;
  }
  .ms-wrap :global(.multiselect button) {
    background: transparent;
    border: none;
    box-shadow: none;
    padding: 0;
    min-height: 0;
  }
  .ms-wrap :global(.multiselect button:hover) {
    transform: none;
    box-shadow: none;
  }
  .ms-wrap :global(.multiselect input) {
    background: transparent;
    border: none;
    box-shadow: none;
    min-height: 0;
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
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    box-shadow: none;
    padding: 6px 10px;
    cursor: pointer;
    color: var(--text-color);
    font-size: 0.85rem;
  }
  .fb-icon {
    flex: none;
    color: var(--text-secondary);
  }
  .fb-entry:hover {
    background: var(--button-hover);
    transform: none;
    box-shadow: none;
  }
  .fb-empty {
    padding: 8px 10px;
    color: var(--text-secondary);
    font-size: 0.8rem;
  }
  .btn-link {
    background: none;
    border: none;
    box-shadow: none;
    color: var(--primary-on-surface);
    cursor: pointer;
    font-size: 0.8rem;
    padding: 0;
  }
  .test-result {
    display: flex;
    align-items: center;
    gap: 6px;
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
    gap: 8px;
  }
  .footer-actions {
    display: flex;
    gap: 8px;
  }
</style>
