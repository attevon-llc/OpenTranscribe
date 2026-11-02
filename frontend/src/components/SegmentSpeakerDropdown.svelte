<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { anchoredPanel } from '$lib/actions/anchoredPanel';
  import { getSpeakerColor } from '$lib/utils/speakerColors';
  import { buildSpeakerMenu, renderProfileMatches } from '$lib/speakers/speakerMenuDom';
  import type { Speaker } from '$lib/types/speaker';
  import type { TranscriptSegment } from '$lib/types/media';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import axiosInstance from '$lib/axios';
  import { listProfiles } from '$lib/api/speakerClusters';
  import { apiCache, CacheTTL } from '$lib/apiCache';
  import { toastStore } from '$stores/toast';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { portal } from '$lib/actions/portal';
  import { isPlaceholderSpeakerName, nextPlaceholderSpeakerName } from '$lib/utils/speakerNames';
  import {
    confirmedSpeakerName,
    inlineNameCheck,
    matchingProfileNames,
    partitionSpeakers,
    pendingSuggestionOf,
    speakerMenuLabel,
    speakerNumberLabelOf,
    uniqueProfileNames
  } from '$lib/speakers/speakerMenu';

  export let segment: TranscriptSegment;
  export let speakers: Speaker[] = [];
  export let mediaFileUuid: string = '';
  // Issue #837 — a transcript search match on the speaker's name/label is counted
  // and navigable but was never visually confirmed. `.search-match`/`.current-match`
  // are the shared highlight classes from src/styles/search.css.
  export let highlighted: boolean = false;
  export let isCurrentMatch: boolean = false;
  // Read-only presentation (e.g. the search-result / "view transcript" modal, issue
  // #755): renders the same chip, but the dropdown never opens and no mutation is
  // possible. Those surfaces may be opened by a viewer-only user.
  export let readOnly: boolean = false;

  const dispatch = createEventDispatcher();
  let triggerButton: HTMLButtonElement;
  let portalContainer: HTMLDivElement | null = null;
  let menuPanel: { destroy: () => void } | null = null;
  let isOpen = false;
  let isCreatingSpeaker = false;

  // The full wire shape (`suggested_name` / `suggestion_source` / `confidence` /
  // `verified`) is declared on `TranscriptSegment['speaker']` in `$lib/types/media.ts`,
  // which is what lets this component tell a human's label apart from a machine's
  // guess.
  $: wireSpeaker = segment.speaker ?? null;

  // `display_name` is the only "a human confirmed this name" field; see `confirmedSpeakerName`
  // for why the resolved_* fields are deliberately not part of this chain (#741).
  $: confirmedName = confirmedSpeakerName(wireSpeaker);
  $: speakerNumberLabel = speakerNumberLabelOf(wireSpeaker, segment.speaker_label);
  $: triggerLabel = translateSpeakerLabel(
    confirmedName || speakerNumberLabel || $t('common.unknown')
  );

  // An unconfirmed suggestion is surfaced as its own row in the menu, with the
  // confidence score, never as the speaker's name.
  $: pendingSuggestion = pendingSuggestionOf(wireSpeaker, confirmedName);

  // --- Name THIS speaker, inline (#1086) ---------------------------------------
  // The common case is "this SPEAKER_02 is Laura": a rename of the speaker the segment
  // already has, for the whole file. It used to need the expanded Edit Speakers section,
  // while the only text field reachable from here was "Add speaker" -- which creates a
  // SECOND speaker. Submitting goes through the same `speakerUpdate` contract the
  // suggestion row and the editor use, so the parent's validation, linked-profile
  // confirmation and rename propagation all apply; this component never writes a name.
  let inlineName = '';
  let profileNames: string[] = [];
  // The field, its Save button, hint and match list live in the imperatively built menu.
  // Typing updates those in place (`refreshInlineNameUi`) rather than rebuilding the menu:
  // replacing the <input> on each keystroke would drop focus and abort an IME composition
  // (Japanese, Korean and Chinese input), which is most of what typing a name is for them.
  let inlineSaveButton: HTMLButtonElement | null = null;
  let inlineHint: HTMLElement | null = null;
  let inlineMatches: HTMLElement | null = null;

  function submitInlineName(raw: string = inlineName) {
    if (!wireSpeaker?.uuid || inlineNameCheck(raw, confirmedName) !== 'ok') return;
    dispatch('speakerUpdate', { speakerId: wireSpeaker.uuid, newName: raw.trim() });
    closeDropdown();
  }

  function loadProfileNames() {
    // Same cache key and shape `prefetchSpeakersData` fills, so a warm cache costs nothing.
    // Autocomplete is a convenience: a failed fetch leaves the field fully usable.
    apiCache
      .getOrFetch('speakers:profiles', () => listProfiles(), CacheTTL.SPEAKERS)
      .then((profiles) => {
        profileNames = uniqueProfileNames(profiles);
        if (isOpen) refreshInlineNameUi();
      })
      .catch(() => {
        profileNames = [];
      });
  }

  function refreshInlineNameUi() {
    const check = inlineNameCheck(inlineName, confirmedName);
    if (inlineSaveButton) inlineSaveButton.disabled = check !== 'ok';
    if (inlineHint) {
      inlineHint.textContent = check === 'placeholder' ? $t('speaker.placeholderNameRejected') : '';
      inlineHint.hidden = check !== 'placeholder';
    }
    if (!inlineMatches) return;

    const matches = matchingProfileNames(profileNames, inlineName);
    renderProfileMatches(inlineMatches, matches, $t('speaker.nameThisSpeakerMatches'), (name) =>
      submitInlineName(name)
    );
  }

  // The next free diarization SLOT for this file (e.g. SPEAKER_03). It is never a
  // name on its own — see `$lib/utils/speakerNames` for the shared contract.
  function getNextSpeakerSlot(): string {
    return nextPlaceholderSpeakerName(speakers.map((s) => s.name));
  }

  // --- "Add speaker": name it FIRST, then create (#740) -----------------------
  // This used to POST the next auto-label on the click itself. The user was never
  // offered a field, and nothing in the segment row changed, so it read as a no-op —
  // and every repeat click minted another orphan SPEAKER_NN row.
  let showCreateModal = false;
  let newSpeakerName = '';
  let newSpeakerSlot = '';

  function openCreateSpeakerModal() {
    if (!mediaFileUuid) return;
    // The dropdown is a body portal that outlives the component's own DOM; leaving
    // it open would float it over the dialog.
    closeDropdown();
    newSpeakerSlot = getNextSpeakerSlot();
    newSpeakerName = '';
    showCreateModal = true;
  }

  function closeCreateSpeakerModal() {
    showCreateModal = false;
    newSpeakerName = '';
  }

  // A typed name that is itself `SPEAKER_NN` is a placeholder, not an identity —
  // accepting it would recreate exactly the state #740 exists to stop.
  $: newSpeakerNameIsPlaceholder =
    newSpeakerName.trim() !== '' && isPlaceholderSpeakerName(newSpeakerName);
  $: canCreateSpeaker =
    !isCreatingSpeaker && newSpeakerName.trim() !== '' && !newSpeakerNameIsPlaceholder;

  async function confirmCreateSpeaker() {
    const typedName = newSpeakerName.trim();
    if (!mediaFileUuid || !canCreateSpeaker) return;

    isCreatingSpeaker = true;
    try {
      // `name` keeps the diarization slot (speaker colours hash it, and the
      // named-vs-auto ordering below reads it); the human's label goes in
      // `display_name`, which is also what marks the new row verified server-side.
      const response = await axiosInstance.post(`/speakers?media_file_uuid=${mediaFileUuid}`, {
        name: newSpeakerSlot,
        display_name: typedName
      });

      const newSpeaker = response.data;

      // Dispatch event to notify parent that a new speaker was created
      dispatch('speakerCreated', { speaker: newSpeaker });

      // Auto-select the new speaker for this segment. The speaker OBJECT rides
      // along: the parent's optimistic patch resolves the uuid against
      // `speakerList`, which cannot contain a speaker created a millisecond ago,
      // so without it the segment renders "Unknown" until a refetch lands — the
      // "I clicked Add speaker and nothing happened" report.
      dispatch('change', {
        segmentUuid: segment.uuid,
        speakerUuid: newSpeaker.uuid,
        speaker: newSpeaker
      });

      closeCreateSpeakerModal();
    } catch (error) {
      console.error('Failed to create new speaker:', error);
      toastStore.error($t('common.somethingWentWrong'));
    } finally {
      isCreatingSpeaker = false;
    }
  }

  function handleCreateModalKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      confirmCreateSpeaker();
    }
  }

  /**
   * Accept an unconfirmed suggestion — one affirmative click, never automatic.
   *
   * Reuses the `speakerUpdate` contract the speaker editor's suggestion chips
   * already dispatch (`{ speakerId, newName }`), so the accept runs the route's
   * existing validation, linked-profile confirmation and rename propagation
   * instead of a second path doing the same job.
   */
  function acceptSuggestion() {
    if (!pendingSuggestion) return;
    dispatch('speakerUpdate', {
      speakerId: pendingSuggestion.uuid,
      newName: pendingSuggestion.name
    });
    closeDropdown();
  }

  // Create portal container on mount
  onMount(() => {
    portalContainer = document.createElement('div');
    portalContainer.className = 'speaker-dropdown-portal';
    document.body.appendChild(portalContainer);
  });

  // The menu is built imperatively into a body portal, so it does not re-render with the
  // component. Without this, an open menu keeps showing the speaker names and the checked
  // row from the moment it was opened — stale the instant a rename lands.
  $: if (isOpen && (speakers || segment)) {
    renderPortal();
  }

  // Cleanup on destroy
  onDestroy(() => {
    if (isOpen) {
      unlockScroll();
    }
    // `remove()` rather than `document.body.removeChild()`: the node may already be
    // detached (navigation tears the body down), and removeChild throws on a non-child.
    menuPanel?.destroy();
    portalContainer?.remove();
    portalContainer = null;
    document.removeEventListener('click', handleGlobalClick, true);
    window.removeEventListener('resize', closeDropdown);
  });

  function closeDropdown() {
    if (isOpen) {
      isOpen = false;
      inlineName = '';
      unlockScroll();
      document.removeEventListener('click', handleGlobalClick, true);
      window.removeEventListener('resize', closeDropdown);
      renderPortal();
    }
  }

  function handleGlobalClick(event: MouseEvent) {
    const target = event.target as Node;
    if (triggerButton?.contains(target)) return;
    if (portalContainer?.contains(target)) return;
    closeDropdown();
  }

  function toggleDropdown(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (readOnly) return;

    isOpen = !isOpen;

    if (isOpen) {
      inlineName = '';
      loadProfileNames();
      lockScroll();
      document.addEventListener('click', handleGlobalClick, true);
      window.addEventListener('resize', closeDropdown);
    } else {
      unlockScroll();
      document.removeEventListener('click', handleGlobalClick, true);
      window.removeEventListener('resize', closeDropdown);
    }

    renderPortal();
  }

  function handleSpeakerSelect(speakerUuid: string | null) {
    dispatch('change', {
      segmentUuid: segment.uuid,
      speakerUuid
    });
    closeDropdown();
  }

  function isCurrentSpeaker(speakerUuid: string): boolean {
    if (!segment.speaker) return false;
    return segment.speaker.uuid === speakerUuid;
  }

  function renderPortal() {
    if (!portalContainer) return;

    if (!isOpen) {
      menuPanel?.destroy();
      menuPanel = null;
      portalContainer.innerHTML = '';
      inlineSaveButton = inlineHint = inlineMatches = null;
      return;
    }

    // The menu is rebuilt from scratch on every external change (a refetched speaker list, a
    // rename landing). If the user is mid-name, put the cursor back where it was afterwards.
    const active = document.activeElement;
    const refocusInlineName =
      active instanceof HTMLInputElement &&
      active.classList.contains('inline-name-input') &&
      portalContainer.contains(active);

    const currentSpeakerUuid = segment.speaker?.uuid;
    // After the focus check above: the old menu holds the focus and destroy() removes it.
    menuPanel?.destroy();
    menuPanel = null;

    const parts = buildSpeakerMenu({
      speakers,
      currentSpeakerUuid,
      noSpeaker: !segment.speaker,
      wireSpeakerUuid: wireSpeaker?.uuid,
      speakerNumberLabel,
      inlineName,
      pendingSuggestion,
      canCreateSpeaker: !!mediaFileUuid,
      t: $t,
      translateLabel: translateSpeakerLabel,
      onInlineInput: (value) => {
        inlineName = value;
        refreshInlineNameUi();
      },
      onSubmitInline: () => submitInlineName(),
      onEscape: closeDropdown,
      onAcceptSuggestion: acceptSuggestion,
      onCreateSpeaker: openCreateSpeakerModal,
      onSelect: handleSpeakerSelect
    });
    const { menu, inlineInput } = parts;
    inlineSaveButton = parts.inlineSaveButton;
    inlineHint = parts.inlineHint;
    inlineMatches = parts.inlineMatches;
    if (inlineInput) refreshInlineNameUi();

    // Clear and append
    portalContainer.innerHTML = '';
    portalContainer.appendChild(menu);
    // The container already lives on <body>; the action only has to place the menu (flip above,
    // cap the height, clamp to the viewport) and keep it placed as the viewport/content changes.
    menuPanel = anchoredPanel(menu, {
      anchor: triggerButton,
      width: 'content',
      gap: 4,
      portal: false,
      maxHeight: 400,
      zIndex: 'var(--z-critical, 10000)',
    });

    if (refocusInlineName && inlineInput) {
      inlineInput.focus();
      inlineInput.setSelectionRange(inlineInput.value.length, inlineInput.value.length);
    }
  }
</script>

<svelte:head>
  <style>
    .speaker-dropdown-portal .dropdown-menu {
      background: var(--surface-color, #1e293b);
      border: 1px solid var(--border-color, #334155);
      border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
      min-width: 200px;
      /* A long suggested name used to widen the menu past the viewport edge, where
         the fixed `left` offset left it unreachable and overlapping the transcript.
         Cap it and let the row labels ellipsise instead (#741). */
      max-width: min(340px, calc(100vw - 24px));
      padding: 4px;
      max-height: 400px;
      overflow-y: auto;
      overflow-x: hidden;
      overscroll-behavior: contain;
    }

    .speaker-dropdown-portal .dropdown-header {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      color: var(--text-secondary, #94a3b8);
      padding: 8px 12px 4px 12px;
      letter-spacing: 0.5px;
    }

    .speaker-dropdown-portal .dropdown-divider {
      height: 1px;
      background: var(--border-light, #475569);
      margin: 4px 0;
    }

    .speaker-dropdown-portal .dropdown-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      padding: 8px 12px;
      background: none;
      border: none;
      border-radius: 6px;
      cursor: pointer;
      transition: background-color 0.15s ease;
      color: var(--text-primary, #f1f5f9);
      font-size: 14px;
      text-align: start;
    }

    .speaker-dropdown-portal .dropdown-item:hover {
      background: var(--surface-hover, #334155);
    }

    .speaker-dropdown-portal .dropdown-item.selected {
      background: rgba(59, 130, 246, 0.15);
    }

    .speaker-dropdown-portal .speaker-option {
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 1;
      min-width: 0;
    }

    .speaker-dropdown-portal .speaker-option span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .speaker-dropdown-portal .speaker-color-indicator {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      border: 1px solid;
      flex-shrink: 0;
    }

    .speaker-dropdown-portal .speaker-color-indicator.no-speaker {
      background: var(--border-color, #475569);
      border-color: var(--border-hover, #64748b);
    }

    .speaker-dropdown-portal .create-speaker-btn {
      color: var(--primary-on-surface);
    }

    .speaker-dropdown-portal .create-speaker-btn:hover {
      background: rgba(59, 130, 246, 0.1);
    }

    /* Every icon in a menu row is a fixed 12px square that never shrinks. They used
       to be three different sizes (12px colour dot, 14px plus, 16px checkmark) with
       no `flex-shrink: 0`, so the rows did not line up and the checkmark on a long
       name was squeezed to a sliver by the label beside it (#741). */
    .speaker-dropdown-portal .dropdown-item > svg,
    .speaker-dropdown-portal .speaker-option > svg {
      width: 12px;
      height: 12px;
      flex-shrink: 0;
    }

    .speaker-dropdown-portal .create-speaker-btn svg {
      color: var(--primary-on-surface);
      margin-right: 4px;
    }

    .speaker-dropdown-portal .suggestion-header {
      color: var(--warning-color, #f59e0b);
    }

    .speaker-dropdown-portal .suggestion-btn svg {
      color: var(--warning-color, #f59e0b);
      margin-right: 4px;
    }

    .speaker-dropdown-portal .suggestion-btn:hover {
      background: var(--surface-hover, #334155);
    }

    .speaker-dropdown-portal .inline-name-row {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px 6px 8px;
    }

    .speaker-dropdown-portal .inline-name-input {
      flex: 1;
      min-width: 0;
      padding: 6px 8px;
      font-size: 13px;
    }

    .speaker-dropdown-portal .inline-name-save {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      width: 28px;
      height: 28px;
      /* The global button rules add padding; on a 28px square that leaves the icon no
         content box at all (computed width 0) and it vanishes. */
      padding: 0;
      box-sizing: border-box;
      color: #fff;
      background: var(--primary-color, #3b82f6);
      border: none;
      border-radius: 6px;
      cursor: pointer;
    }

    .speaker-dropdown-portal .inline-name-save:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }

    .speaker-dropdown-portal .inline-name-save svg {
      width: 14px;
      height: 14px;
      flex-shrink: 0;
    }

    .speaker-dropdown-portal .inline-name-hint {
      padding: 0 12px 6px 12px;
      font-size: 11px;
      line-height: 1.35;
      color: var(--warning-color, #f59e0b);
    }

    .speaker-dropdown-portal .inline-name-matches[hidden],
    .speaker-dropdown-portal .inline-name-hint[hidden] {
      display: none;
    }

    .speaker-dropdown-portal .inline-name-match {
      justify-content: flex-start;
    }

    .speaker-dropdown-portal .suggestion-confidence {
      flex-shrink: 0;
      margin-left: 8px;
      font-size: 11px;
      font-weight: 600;
      color: var(--text-secondary, #94a3b8);
      font-variant-numeric: tabular-nums;
    }
  </style>
</svelte:head>

<div class="speaker-dropdown-container">
  <button
    class="speaker-trigger"
    class:read-only={readOnly}
    bind:this={triggerButton}
    on:click={toggleDropdown}
    aria-disabled={readOnly}
    title={readOnly ? triggerLabel : $t('speaker.clickToChangeSpeaker')}
  >
    <div
      class="segment-speaker"
      class:search-match={highlighted && !isCurrentMatch}
      class:current-match={isCurrentMatch}
      style="background-color: {getSpeakerColor(segment.speaker?.name || segment.speaker_label || 'Unknown').bg}; border-color: {getSpeakerColor(segment.speaker?.name || segment.speaker_label || 'Unknown').border}; --speaker-light: {getSpeakerColor(segment.speaker?.name || segment.speaker_label || 'Unknown').textLight}; --speaker-dark: {getSpeakerColor(segment.speaker?.name || segment.speaker_label || 'Unknown').textDark};"
    >
      {triggerLabel}
    </div>
    {#if pendingSuggestion && !readOnly}
      <!-- The suggestion exists but is unconfirmed: a marker, not a name. -->
      <span class="suggestion-marker" title={$t('speaker.unconfirmedSuggestion')} aria-hidden="true">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
        </svg>
      </span>
    {/if}
    {#if !readOnly}
    <svg
      class="dropdown-arrow"
      class:open={isOpen}
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
    >
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>
    {/if}
  </button>
</div>

<!--
  Portaled to <body> deliberately. This component renders inside a transcript
  segment's `<button class="segment-content">`, so an in-place dialog is both
  invalid HTML (interactive content inside a button) and visually broken: the
  backdrop's `position: fixed` resolved `width/height: 100%` against the 548px
  segment row, not the viewport, so the dialog rendered as a strip wedged into
  the transcript row with no backdrop and no way to use it (#740).

  `closeOnBackdropClick={false}` because the dialog holds a half-typed name.
-->
<div use:portal>
  <BaseModal
    isOpen={showCreateModal}
    title={$t('speaker.newSpeakerTitle')}
    size="sm"
    closeOnBackdropClick={false}
    onClose={closeCreateSpeakerModal}
  >
  <label class="new-speaker-field">
    <span class="new-speaker-label">{$t('speaker.newSpeakerNameLabel')}</span>
    <!-- svelte-ignore a11y-autofocus -->
    <input
      class="new-speaker-name-input"
      type="text"
      autofocus
      maxlength="100"
      bind:value={newSpeakerName}
      on:keydown={handleCreateModalKeydown}
      placeholder={$t('speaker.newSpeakerNamePlaceholder')}
    />
  </label>
  {#if newSpeakerNameIsPlaceholder}
    <p class="new-speaker-error">{$t('speaker.placeholderNameRejected')}</p>
  {:else}
    <p class="new-speaker-hint">{$t('speaker.newSpeakerSlotHint', { label: newSpeakerSlot })}</p>
  {/if}
  <svelte:fragment slot="footer">
    <!-- `modal-button` / `modal-cancel-button` / `modal-primary-button` are the
         global classes in `src/styles/form-elements.css`; the `create-speaker-*`
         classes are selector hooks only and carry no styling of their own. -->
    <button
      type="button"
      class="modal-button modal-cancel-button create-speaker-cancel"
      on:click={closeCreateSpeakerModal}
    >
      {$t('common.cancel')}
    </button>
    <button
      type="button"
      class="modal-button modal-primary-button create-speaker-confirm"
      on:click={confirmCreateSpeaker}
      disabled={!canCreateSpeaker}
    >
      {isCreatingSpeaker ? $t('common.saving') : $t('speaker.addSpeaker')}
    </button>
  </svelte:fragment>
</BaseModal>
</div>

<style>
  .speaker-dropdown-container {
    position: relative;
    display: inline-block;
    /* Without these the container refuses to shrink below its content, so a long
       name pushed the transcript text sideways instead of ellipsising (#741). */
    max-width: 100%;
    min-width: 0;
  }

  .speaker-trigger {
    display: flex;
    align-items: center;
    gap: 4px;
    background: none;
    border: none;
    cursor: pointer;
    padding: 0;
    max-width: 100%;
    min-width: 0;
  }

  .speaker-trigger.read-only {
    cursor: default;
  }

  .segment-speaker {
    font-size: 12px;
    font-weight: var(--font-weight-semibold);
    padding: 2px 8px;
    border-radius: 12px;
    /* A confirmed name is shown in full: it wraps inside the pill instead of being cut to
       "UNKNOWN (NOT …" (#1149). `overflow-wrap: anywhere` also breaks an unbroken token. */
    white-space: normal;
    overflow-wrap: anywhere;
    line-height: 1.25;
    max-width: 180px;
    /* Pairs with the flex parent: `max-width` alone caps growth but a flex item's
       automatic minimum size is its content, so the pill still refused to shrink
       and the arrow was pushed out of the row. */
    min-width: 0;
    border: 1px solid;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: all 0.2s ease;
    color: var(--speaker-light);
  }

  :global([data-theme='dark']) .segment-speaker {
    color: var(--speaker-dark);
  }

  /* Issue #837 — the chip's own inline background-color (speaker colour) would win
     over .search-match/.current-match's background (src/styles/search.css), since an
     inline style always beats a class selector. A ring is layered on top instead so
     the highlight is visible without fighting the speaker colour. */
  .segment-speaker.search-match {
    box-shadow: 0 0 0 2px rgba(250, 204, 21, 0.85);
  }

  .segment-speaker.current-match {
    box-shadow: 0 0 0 2px rgba(249, 115, 22, 0.9);
  }

  .dropdown-arrow {
    color: var(--text-secondary);
    transition: transform 0.2s ease;
    flex-shrink: 0;
  }

  .dropdown-arrow.open {
    transform: rotate(180deg);
  }

  .speaker-trigger:hover .segment-speaker {
    opacity: 0.8;
  }

  /* Marks "there is an unconfirmed suggestion for this speaker" WITHOUT putting the
     guess in the name slot — the suggestion itself lives in the menu (#741). */
  .suggestion-marker {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 12px;
    height: 12px;
    flex-shrink: 0;
    color: var(--warning-color, #f59e0b);
  }

  .new-speaker-field {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .new-speaker-label {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-secondary);
  }

  /* The input itself is deliberately unstyled here: `src/styles/form-elements.css`
     already themes every `input` for light and dark. */

  .new-speaker-hint {
    margin: 0.625rem 0 0;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .new-speaker-error {
    margin: 0.625rem 0 0;
    font-size: 0.75rem;
    color: var(--error-color);
  }
</style>
