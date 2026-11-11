<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
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
    type Capabilities,
    type DirectoryListing,
  } from '$lib/api/watchSourcesApi';
  import { listTags } from '$lib/api/tags';
  import {
    blankWatchSourceForm,
    buildWatchSourcePayload,
    formFromSource,
    type WatchSourceForm,
  } from '$lib/settings/watchSourceForm';

  export let show = false;
  export let editingSource: WatchSource | null = null;
  export let capabilities: Capabilities = {
    watch_source_enabled: true,
    local_enabled: false,
    fs_events_enabled: false,
    fs_events_mode: 'auto',
  };

  const dispatch = createEventDispatcher();

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

  const blankForm = (): WatchSourceForm =>
    blankWatchSourceForm(capabilities.local_enabled ? 'local' : 's3');

  let form: WatchSourceForm = blankForm();

  onMount(() => {
    void (async () => {
      try {
        const [colRes, libraryTags] = await Promise.all([axiosInstance.get('/collections'), listTags()]);
        availableCollections = colRes.data ?? [];
        colOptions = availableCollections.map((c) => ({ label: c.name, value: c.uuid }));
        tagNames = libraryTags.map((tag) => tag.name);
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
    form = formFromSource(src, blankForm());
    selectedTags = [...(src.tag_names ?? [])];
    mapEditingCollections();
  }

  // Initialize the form when the modal transitions to open, so blankForm() reads
  // the real `capabilities` prop (which arrives after init) — otherwise new
  // sources would wrongly default to S3 even when a local folder is available.
  let prevShow = false;
  function syncOpenState(open: boolean) {
    if (!open) {
      prevShow = false;
      return;
    }
    if (prevShow) return;
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
  $: syncOpenState(show);

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
    return buildWatchSourcePayload(form, selectedTags, collectionIds);
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

<BaseModal isOpen={show} onClose={handleClose} size="lg">
  <svelte:fragment slot="header">
    <h2 class="modal-title">
      {editingSource
        ? $t('settings.watchSources.editTitle')
        : $t('settings.watchSources.addTitle')}
    </h2>
  </svelte:fragment>

  <div class="ws-form">
    <FormField id="ws-name" label={$t('settings.watchSources.fields.name')} labelSize="md">
      <input
        id="ws-name"
        type="text"
        class="form-input"
        bind:value={form.name}
        placeholder={$t('settings.watchSources.fields.namePlaceholder')}
      />
    </FormField>

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
        <FormField id="ws-type" label={$t('settings.watchSources.fields.sourceType')} labelSize="md">
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
        </FormField>

        {#if form.source_type === 'local'}
          <FormField id="ws-localpath" label={$t('settings.watchSources.fields.localPath')} labelSize="md">
            <div class="path-row">
              <input dir="ltr" id="ws-localpath" type="text" class="form-input" bind:value={form.local_path} placeholder={$t('settings.watchSources.fields.localPathPlaceholder')} />
              <button type="button" class="btn btn-secondary" on:click={() => openBrowser(form.local_path)}>{$t('settings.watchSources.browse')}</button>
            </div>
            {#if browsing}
              <Spinner label={$t('common.loading')} size="small" />
            {:else if listing}
              <div class="folder-browser">
                <div class="fb-header">
                  <span class="fb-current">/{listing.current_path}</span>
                  <button type="button" class="btn btn-link" on:click={() => selectFolder(listing?.current_path ?? '')}>{$t('settings.watchSources.selectThisFolder')}</button>
                </div>
                {#if listing.parent_path !== null && listing.parent_path !== undefined}
                  <button type="button" class="fb-entry" on:click={() => openBrowser(listing?.parent_path ?? '')}>.. ({$t('settings.watchSources.parent')})</button>
                {/if}
                {#each listing.directories as dir (dir.path)}
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
          </FormField>
          <label class="checkbox-row">
            <input type="checkbox" bind:checked={form.delete_after_import} />
            <span>{$t('settings.watchSources.fields.deleteAfterImport')}</span>
          </label>
          {#if form.delete_after_import}
            <p class="warning-text">{$t('settings.watchSources.deleteWarning')}</p>
          {/if}
        {:else if form.source_type === 's3'}
          <FormField id="ws-s3ep" label={$t('settings.watchSources.fields.s3Endpoint')} labelSize="md">
            <input dir="ltr" id="ws-s3ep" type="text" class="form-input" bind:value={form.s3_endpoint_url} placeholder="https://s3.amazonaws.com (blank = AWS)" />
          </FormField>
          <div class="form-row">
            <FormField id="ws-s3b" label={$t('settings.watchSources.fields.s3Bucket')} labelSize="md">
              <input dir="ltr" id="ws-s3b" type="text" class="form-input" bind:value={form.s3_bucket_name} />
            </FormField>
            <FormField id="ws-s3p" label={$t('settings.watchSources.fields.s3Prefix')} labelSize="md">
              <input dir="ltr" id="ws-s3p" type="text" class="form-input" bind:value={form.s3_prefix} />
            </FormField>
          </div>
          <div class="form-row">
            <FormField id="ws-s3r" label={$t('settings.watchSources.fields.s3Region')} labelSize="md">
              <input dir="ltr" id="ws-s3r" type="text" class="form-input" bind:value={form.s3_region} />
            </FormField>
            <FormField id="ws-s3ak" label={$t('settings.watchSources.fields.s3AccessKey')} labelSize="md">
              <input dir="ltr" id="ws-s3ak" type="text" class="form-input" bind:value={form.s3_access_key_id} />
            </FormField>
          </div>
          <FormField id="ws-s3sk" label={$t('settings.watchSources.fields.s3SecretKey')} labelSize="md">
            <input dir="ltr" id="ws-s3sk" type="password" class="form-input" bind:value={form.s3_secret_key} placeholder={editingSource?.has_s3_secret_key ? $t('settings.watchSources.secretStored') : ''} />
          </FormField>
          <label class="checkbox-row">
            <input type="checkbox" bind:checked={form.s3_use_ssl} />
            <span>{$t('settings.watchSources.fields.useSsl')}</span>
          </label>
        {:else if form.source_type === 'smb'}
          <div class="form-row">
            <FormField id="ws-smbs" label={$t('settings.watchSources.fields.smbServer')} labelSize="md">
              <input dir="ltr" id="ws-smbs" type="text" class="form-input" bind:value={form.smb_server} />
            </FormField>
            <FormField id="ws-smbsh" label={$t('settings.watchSources.fields.smbShare')} labelSize="md">
              <input dir="ltr" id="ws-smbsh" type="text" class="form-input" bind:value={form.smb_share} />
            </FormField>
          </div>
          <FormField id="ws-smbp" label={$t('settings.watchSources.fields.smbPath')} labelSize="md">
            <input dir="ltr" id="ws-smbp" type="text" class="form-input" bind:value={form.smb_path} />
          </FormField>
          <div class="form-row">
            <FormField id="ws-smbu" label={$t('settings.watchSources.fields.smbUsername')} labelSize="md">
              <input dir="ltr" id="ws-smbu" type="text" class="form-input" bind:value={form.smb_username} />
            </FormField>
            <FormField id="ws-smbpw" label={$t('settings.watchSources.fields.smbPassword')} labelSize="md">
              <input dir="ltr" id="ws-smbpw" type="password" class="form-input" bind:value={form.smb_password} placeholder={editingSource?.has_smb_password ? $t('settings.watchSources.secretStored') : ''} />
            </FormField>
          </div>
          <div class="form-row">
            <FormField id="ws-smbd" label={$t('settings.watchSources.fields.smbDomain')} labelSize="md">
              <input dir="ltr" id="ws-smbd" type="text" class="form-input" bind:value={form.smb_domain} />
            </FormField>
            <FormField id="ws-smbport" label={$t('settings.watchSources.fields.smbPort')} labelSize="md">
              <input id="ws-smbport" type="number" class="form-input" bind:value={form.smb_port} />
            </FormField>
          </div>
        {/if}
      </div>
    {/if}

    <!-- Step: Processing -->
    {#if currentStep?.id === 'processing'}
      <div class="step-panel">
        <div class="form-row">
          <FormField id="ws-interval" label={$t('settings.watchSources.fields.pollingInterval')} labelSize="md">
            <input id="ws-interval" type="number" min="1" max="1440" class="form-input" bind:value={form.polling_interval_minutes} />
          </FormField>
          <FormField id="ws-age" label={$t('settings.watchSources.fields.skipOlderThan')} labelSize="md" hint={$t('settings.watchSources.skipOlderHelp')} let:describedby>
            <input id="ws-age" aria-describedby={describedby} type="number" min="0" class="form-input" bind:value={form.skip_files_older_than_days} />
          </FormField>
        </div>
        <FormField id="ws-ext" label={$t('settings.watchSources.fields.fileExtensions')} labelSize="md" hint={$t('settings.watchSources.extensionsHelp')} let:describedby>
          <input dir="ltr" id="ws-ext" aria-describedby={describedby} type="text" class="form-input" bind:value={form.file_extensions} placeholder=".mp4,.mp3,.wav" />
        </FormField>
        <div class="form-row">
          <FormField id="ws-min" label={$t('settings.watchSources.fields.minSpeakers')} labelSize="md">
            <input id="ws-min" type="number" min="1" class="form-input" bind:value={form.min_speakers} placeholder={$t('settings.watchSources.fields.speakersPlaceholder')} />
          </FormField>
          <FormField id="ws-max" label={$t('settings.watchSources.fields.maxSpeakers')} labelSize="md">
            <input id="ws-max" type="number" min="1" class="form-input" bind:value={form.max_speakers} placeholder={$t('settings.watchSources.fields.speakersPlaceholder')} />
          </FormField>
        </div>
        <p class="field-hint">{$t('settings.watchSources.speakersHelp')}</p>
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
          <FormField id="ws-regex" label={$t('settings.watchSources.fields.multipartRegex')} labelSize="md" hint={$t('settings.watchSources.regexHelp')} let:describedby>
            <input dir="ltr" id="ws-regex" aria-describedby={describedby} type="text" class="form-input" bind:value={form.multipart_regex} />
          </FormField>
          <FormField id="ws-regextest" label={$t('settings.watchSources.regexTester')} labelSize="md">
            <div class="path-row">
              <input dir="ltr" id="ws-regextest" type="text" class="form-input" bind:value={regexSample} placeholder="meeting_P001.mp4" />
              <button type="button" class="btn btn-secondary" on:click={runRegexTest}>{$t('settings.watchSources.testRegex')}</button>
            </div>
            {#if regexResult}<p class="field-hint">{regexResult}</p>{/if}
          </FormField>
          <div class="form-row">
            <FormField id="ws-mpw" label={$t('settings.watchSources.fields.multipartWindow')} labelSize="md">
              <input id="ws-mpw" type="number" min="1" class="form-input" bind:value={form.multipart_time_window_hours} />
            </FormField>
            <FormField id="ws-mpws" label={$t('settings.watchSources.fields.multipartWaitScans')} labelSize="md">
              <input id="ws-mpws" type="number" min="1" class="form-input" bind:value={form.multipart_wait_scans} />
            </FormField>
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
        <FormField id="ws-tags" label={$t('settings.watchSources.fields.tags')} labelSize="md">
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
        </FormField>
        <FormField id="ws-cols" label={$t('settings.watchSources.collectionsLabel')} labelSize="md">
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
        </FormField>
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
    gap: var(--space-3-5);
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
    padding: var(--space-1) 0 var(--space-2);
  }
  .step-item {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
    padding: var(--space-1) var(--space-1-5);
    background: transparent;
    border: none;
    border-radius: var(--radius-md);
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
    border-radius: var(--radius-round);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-semibold);
    border: 2px solid var(--border-color);
    background: var(--surface-color);
    color: var(--text-secondary);
    flex-shrink: 0;
    transition: all var(--duration-base) ease;
  }
  .step-item.active .step-dot,
  .step-item.completed .step-dot {
    border-color: var(--primary-color);
    background: var(--primary-color);
    color: var(--text-on-solid);
  }
  .step-item.visited .step-dot {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
  }
  .step-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    white-space: nowrap;
  }
  .step-item.active .step-label {
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-semibold);
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
    margin: 0 var(--space-1);
  }
  .step-line.completed {
    background: var(--primary-color);
  }

  .step-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-height: 180px;
  }
  .form-row > :global(.form-field) {
    flex: 1;
  }
  .form-row {
    display: flex;
    gap: var(--space-3);
  }
  /* .form-input / .form-select inherit the app's global input/select styling. */
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-body);
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
    margin-top: var(--space-1);
  }
  .path-row {
    display: flex;
    gap: var(--space-2);
  }
  .path-row .form-input {
    flex: 1;
  }
  .field-hint {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: var(--space-0-5) 0 0;
  }
  .step-hint {
    font-size: var(--font-size-md);
    color: var(--text-secondary);
    margin: 0;
    line-height: 1.5;
  }
  /* Theme svelte-multiselect to match the app's inputs (light/dark) and
     neutralize the aggressive global button styling on its internal buttons. */
  .ms-wrap :global(.multiselect) {
    --sms-bg: var(--input-background);
    --sms-border: 1px solid var(--field-border);
    --sms-focus-border: 1px solid var(--focus-ring-color);
    --sms-text-color: var(--text-color);
    --sms-placeholder-color: var(--text-secondary);
    --sms-options-bg: var(--surface-color);
    --sms-li-active-bg: var(--button-hover);
    --sms-selected-bg: rgba(var(--primary-color-rgb), 0.15);
    --sms-selected-text-color: var(--text-color);
    border-radius: var(--radius-sm);
    min-height: 40px;
    font-size: var(--font-size-body);
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
    font-size: var(--font-size-sm);
    color: var(--warning-text);
    margin: 0;
  }
  .folder-browser {
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    max-height: 200px;
    overflow-y: auto;
    margin-top: var(--space-1-5);
  }
  .fb-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: var(--space-1-5) var(--space-2-5);
    border-bottom: 1px solid var(--border-color);
    background: var(--button-hover);
  }
  .fb-current {
    font-family: monospace;
    font-size: var(--font-size-sm);
  }
  .fb-entry {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    box-shadow: none;
    padding: var(--space-1-5) var(--space-2-5);
    cursor: pointer;
    color: var(--text-color);
    font-size: var(--font-size-md);
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
    padding: var(--space-2) var(--space-2-5);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }
  .test-result {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    padding: var(--space-2) var(--space-2-5);
    border-radius: var(--radius-md);
    font-size: var(--font-size-md);
  }
  .test-result.success {
    background: rgba(var(--success-color-rgb), 0.12);
    color: var(--success-text);
  }
  .test-result.failure {
    background: rgba(var(--error-color-rgb), 0.12);
    color: var(--error-text);
  }
  .footer-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    gap: var(--space-2);
  }
  .footer-actions {
    display: flex;
    gap: var(--space-2);
  }
</style>
