<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { getSpeakerColor } from '$lib/utils/speakerColors';
  import type { Speaker, Segment } from '$lib/types/speaker';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import axiosInstance from '$lib/axios';
  import { listProfiles } from '$lib/api/speakerClusters';
  import { apiCache, CacheTTL } from '$lib/apiCache';
  import { toastStore } from '$stores/toast';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { portal } from '$lib/actions/portal';
  import {
    isPlaceholderSpeakerName,
    nextPlaceholderSpeakerName,
    placeholderSpeakerNumber
  } from '$lib/utils/speakerNames';

  export let segment: Segment;
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
  let isOpen = false;
  let isCreatingSpeaker = false;

  // The full wire shape (`suggested_name` / `suggestion_source` / `confidence` /
  // `verified`) is declared on `Segment['speaker']` in `$lib/types/speaker.ts`,
  // which is what lets this component tell a human's label apart from a machine's
  // guess. This file used to carry a local copy of that interface; the copy is
  // deleted rather than left alongside the shared one.
  $: wireSpeaker = segment.speaker ?? null;

  // `display_name` is the ONLY field that means "a human confirmed this name":
  // `POST /speakers` and `PUT /speakers/{uuid}` both flip `verified` the moment it
  // is set, and nothing else writes it without an affirmative click.
  //
  // Deliberately NOT part of this chain: `speaker.resolved_display_name` and
  // `segment.resolved_speaker_name`. Both are `canonical_speaker_label(...)`, which
  // returns `suggested_name` once confidence >= 0.75 — i.e. they intentionally
  // collapse "confirmed" and "guessed" into one string. Rendering either one here
  // would put an unverified LLM guess in the name slot, which is exactly what the
  // repo rule "LLM speaker-ID suggestions are never auto-applied" forbids (#741).
  $: confirmedName = (wireSpeaker?.display_name ?? '').trim();
  $: speakerNumberLabel = wireSpeaker?.name || segment.speaker_label || '';
  $: triggerLabel = translateSpeakerLabel(
    confirmedName || speakerNumberLabel || $t('common.unknown')
  );

  // An unconfirmed suggestion is surfaced as its own row in the menu, with the
  // confidence score, never as the speaker's name.
  $: pendingSuggestion =
    !confirmedName && wireSpeaker?.suggested_name && wireSpeaker.uuid
      ? {
          uuid: wireSpeaker.uuid,
          name: wireSpeaker.suggested_name,
          confidence: wireSpeaker.confidence ?? 0
        }
      : null;

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

  function inlineNameCheck(raw: string): 'ok' | 'empty' | 'placeholder' | 'unchanged' {
    const name = raw.trim();
    if (name === '') return 'empty';
    // `SPEAKER_NN` is a diarization slot, not an identity -- same rule as "Add speaker".
    if (isPlaceholderSpeakerName(name)) return 'placeholder';
    if (name === confirmedName) return 'unchanged';
    return 'ok';
  }

  function submitInlineName(raw: string = inlineName) {
    if (!wireSpeaker?.uuid || inlineNameCheck(raw) !== 'ok') return;
    dispatch('speakerUpdate', { speakerId: wireSpeaker.uuid, newName: raw.trim() });
    closeDropdown();
  }

  function loadProfileNames() {
    // Same cache key and shape `prefetchSpeakersData` fills, so a warm cache costs nothing.
    // Autocomplete is a convenience: a failed fetch leaves the field fully usable.
    apiCache
      .getOrFetch('speakers:profiles', () => listProfiles(), CacheTTL.SPEAKERS)
      .then((profiles) => {
        profileNames = [...new Set((profiles ?? []).map((p) => p.name).filter(Boolean))];
        if (isOpen) refreshInlineNameUi();
      })
      .catch(() => {
        profileNames = [];
      });
  }

  function refreshInlineNameUi() {
    const check = inlineNameCheck(inlineName);
    if (inlineSaveButton) inlineSaveButton.disabled = check !== 'ok';
    if (inlineHint) {
      inlineHint.textContent = check === 'placeholder' ? $t('speaker.placeholderNameRejected') : '';
      inlineHint.hidden = check !== 'placeholder';
    }
    if (!inlineMatches) return;

    const typed = inlineName.trim().toLowerCase();
    const matches =
      typed === ''
        ? []
        : profileNames
            .filter((name) => name.toLowerCase().includes(typed))
            // The API's order is unspecified; a stable one keeps the list from shuffling.
            .sort((a, b) => a.localeCompare(b))
            .slice(0, 5);
    inlineMatches.replaceChildren();
    inlineMatches.hidden = matches.length === 0;
    if (matches.length === 0) return;

    const label = document.createElement('div');
    label.className = 'dropdown-header inline-name-matches-label';
    label.textContent = $t('speaker.nameThisSpeakerMatches');
    inlineMatches.appendChild(label);
    for (const name of matches) {
      const pick = document.createElement('button');
      pick.className = 'dropdown-item inline-name-match';
      pick.dataset.action = 'pick-profile';
      pick.textContent = name; // textContent is XSS-safe
      pick.addEventListener('click', (e) => {
        e.stopPropagation();
        submitInlineName(name);
      });
      inlineMatches.appendChild(pick);
    }
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

  function getMenuPosition(): { top: number; left: number; openUpward: boolean } {
    if (!triggerButton) return { top: 0, left: 0, openUpward: false };

    const rect = triggerButton.getBoundingClientRect();
    // Estimate menu height: header + no speaker + divider + speakers + divider + create button
    const itemHeight = 36;
    const headerHeight = 28;
    const dividerHeight = 9;
    const suggestionHeight = pendingSuggestion ? headerHeight + itemHeight + dividerHeight : 0;
    const inlineNameHeight = wireSpeaker?.uuid ? 52 : 0;
    const estimatedHeight = headerHeight + itemHeight + dividerHeight + (speakers.length * itemHeight) + (mediaFileUuid ? dividerHeight + itemHeight : 0) + suggestionHeight + inlineNameHeight;

    const viewportHeight = window.innerHeight;
    const spaceBelow = viewportHeight - rect.bottom - 8;
    const spaceAbove = rect.top - 8;

    // Open upward if not enough space below and more space above
    const openUpward = spaceBelow < estimatedHeight && spaceAbove > spaceBelow;

    let top: number;
    if (openUpward) {
      top = rect.top - 4;
    } else {
      top = rect.bottom + 4;
    }

    return { top, left: rect.left, openUpward };
  }

  // Helper to create SVG checkmark element
  function createCheckmarkSvg(): SVGSVGElement {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '16');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    polyline.setAttribute('points', '20 6 9 17 4 12');
    svg.appendChild(polyline);
    return svg;
  }

  // Helper to create SVG plus icon element
  function createPlusSvg(): SVGSVGElement {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    const line1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line1.setAttribute('x1', '12');
    line1.setAttribute('y1', '5');
    line1.setAttribute('x2', '12');
    line1.setAttribute('y2', '19');
    const line2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line2.setAttribute('x1', '5');
    line2.setAttribute('y1', '12');
    line2.setAttribute('x2', '19');
    line2.setAttribute('y2', '12');
    svg.appendChild(line1);
    svg.appendChild(line2);
    return svg;
  }

  // Helper to create the "AI suggestion" sparkle icon element
  function createSparkleSvg(): SVGSVGElement {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '12');
    svg.setAttribute('height', '12');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'currentColor');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute(
      'd',
      'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z'
    );
    svg.appendChild(path);
    return svg;
  }

  // Helper to create a speaker dropdown item button using DOM methods (XSS-safe)
  function createDropdownItem(
    speakerUuid: string,
    label: string,
    isSelected: boolean,
    colorBg?: string,
    colorBorder?: string,
    isNoSpeaker: boolean = false
  ): HTMLButtonElement {
    const button = document.createElement('button');
    button.className = `dropdown-item${isSelected ? ' selected' : ''}`;
    button.dataset.speakerUuid = speakerUuid;

    const speakerOption = document.createElement('div');
    speakerOption.className = 'speaker-option';

    const colorIndicator = document.createElement('div');
    colorIndicator.className = `speaker-color-indicator${isNoSpeaker ? ' no-speaker' : ''}`;
    if (colorBg && colorBorder) {
      colorIndicator.style.backgroundColor = colorBg;
      colorIndicator.style.borderColor = colorBorder;
    }

    const span = document.createElement('span');
    span.textContent = label; // textContent is XSS-safe

    speakerOption.appendChild(colorIndicator);
    speakerOption.appendChild(span);
    button.appendChild(speakerOption);

    if (isSelected) {
      button.appendChild(createCheckmarkSvg());
    }

    return button;
  }

  function renderPortal() {
    if (!portalContainer) return;

    if (!isOpen) {
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

    const pos = getMenuPosition();
    const currentSpeakerUuid = segment.speaker?.uuid;

    // Build menu using DOM methods to prevent XSS
    const menu = document.createElement('div');
    menu.className = `dropdown-menu${pos.openUpward ? ' open-upward' : ''}`;
    menu.style.top = `${pos.top}px`;
    menu.style.left = `${pos.left}px`;
    if (pos.openUpward) {
      menu.style.transform = 'translateY(-100%)';
    }

    // Header
    const header = document.createElement('div');
    header.className = 'dropdown-header';
    header.textContent = $t('speaker.assignSpeaker');
    menu.appendChild(header);

    // Name THIS speaker (#1086): a field first, because it is the common action.
    let inlineInput: HTMLInputElement | null = null;
    if (wireSpeaker?.uuid) {
      const row = document.createElement('div');
      row.className = 'inline-name-row';

      inlineInput = document.createElement('input');
      inlineInput.type = 'text';
      inlineInput.className = 'inline-name-input';
      inlineInput.maxLength = 255;
      inlineInput.autocomplete = 'off';
      // A name typed in Arabic must align right even inside an LTR page, and vice versa.
      inlineInput.dir = 'auto';
      inlineInput.placeholder = $t('speaker.nameThisSpeakerPlaceholder', {
        label: translateSpeakerLabel(speakerNumberLabel || $t('common.unknown'))
      });
      inlineInput.setAttribute('aria-label', $t('speaker.nameThisSpeakerLabel'));
      inlineInput.value = inlineName;
      const input = inlineInput;
      input.addEventListener('input', () => {
        inlineName = input.value;
        refreshInlineNameUi();
      });
      input.addEventListener('keydown', (e) => {
        // Typing must not reach the page's own shortcuts (play/pause, seek, ...).
        e.stopPropagation();
        if (e.key === 'Enter') {
          e.preventDefault();
          submitInlineName();
        } else if (e.key === 'Escape') {
          e.preventDefault();
          closeDropdown();
        }
      });
      row.appendChild(input);

      const save = document.createElement('button');
      save.className = 'inline-name-save';
      save.dataset.action = 'save-inline-name';
      save.setAttribute('aria-label', $t('speaker.nameThisSpeakerSave'));
      save.title = $t('speaker.nameThisSpeakerSave');
      save.appendChild(createCheckmarkSvg());
      save.addEventListener('click', (e) => {
        e.stopPropagation();
        submitInlineName();
      });
      row.appendChild(save);
      inlineSaveButton = save;
      menu.appendChild(row);

      const hint = document.createElement('div');
      hint.className = 'inline-name-hint';
      hint.hidden = true;
      menu.appendChild(hint);
      inlineHint = hint;

      const matchList = document.createElement('div');
      matchList.className = 'inline-name-matches';
      matchList.hidden = true;
      menu.appendChild(matchList);
      inlineMatches = matchList;

      const nameDivider = document.createElement('div');
      nameDivider.className = 'dropdown-divider';
      menu.appendChild(nameDivider);
      refreshInlineNameUi();
    } else {
      inlineSaveButton = inlineHint = inlineMatches = null;
    }

    // Unconfirmed LLM / voice-match suggestion — offered HERE, with its confidence
    // score, and never in the name slot (#741). One affirmative click accepts it.
    if (pendingSuggestion) {
      const suggestionHeader = document.createElement('div');
      suggestionHeader.className = 'dropdown-header suggestion-header';
      suggestionHeader.textContent = $t('speaker.unconfirmedSuggestion');
      menu.appendChild(suggestionHeader);

      const suggestionBtn = document.createElement('button');
      suggestionBtn.className = 'dropdown-item suggestion-btn';
      suggestionBtn.dataset.action = 'accept-suggestion';
      suggestionBtn.title = $t('speaker.acceptSuggestionTitle');

      const suggestionOption = document.createElement('div');
      suggestionOption.className = 'speaker-option';
      suggestionOption.appendChild(createSparkleSvg());

      const suggestionSpan = document.createElement('span');
      suggestionSpan.textContent = pendingSuggestion.name;
      suggestionOption.appendChild(suggestionSpan);
      suggestionBtn.appendChild(suggestionOption);

      const confidenceBadge = document.createElement('span');
      confidenceBadge.className = 'suggestion-confidence';
      confidenceBadge.textContent = `${Math.round(pendingSuggestion.confidence * 100)}%`;
      suggestionBtn.appendChild(confidenceBadge);

      suggestionBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        acceptSuggestion();
      });
      menu.appendChild(suggestionBtn);

      const suggestionDivider = document.createElement('div');
      suggestionDivider.className = 'dropdown-divider';
      menu.appendChild(suggestionDivider);
    }

    // "Add speaker" — top of list for quick access. Opens the naming dialog; the
    // speaker is not created until the user has typed a name (#740).
    if (mediaFileUuid) {
      const createBtn = document.createElement('button');
      createBtn.className = 'dropdown-item create-speaker-btn';
      createBtn.dataset.action = 'create-speaker';

      const createOption = document.createElement('div');
      createOption.className = 'speaker-option';
      createOption.appendChild(createPlusSvg());

      const createSpan = document.createElement('span');
      createSpan.textContent = $t('speaker.newSpeakerForSegment');
      createOption.appendChild(createSpan);

      createBtn.appendChild(createOption);
      createBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openCreateSpeakerModal();
      });
      menu.appendChild(createBtn);
    }

    // "No Speaker" option
    const noSpeakerBtn = createDropdownItem(
      '',
      $t('speaker.noSpeaker'),
      !segment.speaker,
      undefined,
      undefined,
      true
    );
    noSpeakerBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      handleSpeakerSelect(null);
    });
    menu.appendChild(noSpeakerBtn);

    // Divider — separates utility actions from named speakers
    const divider = document.createElement('div');
    divider.className = 'dropdown-divider';
    menu.appendChild(divider);

    // Split speakers: human-named first, then unidentified SPEAKER_## auto-labels.
    // Classification uses the effective display name so that a speaker renamed to
    // "John Smith" (display_name) is treated as named even though their internal
    // name is still "SPEAKER_01".
    const isAutoLabel = (s: Speaker) => isPlaceholderSpeakerName(s.display_name || s.name);
    const namedSpeakers = speakers.filter((s) => !isAutoLabel(s));
    const autoSpeakers = speakers.filter((s) => isAutoLabel(s));

    const renderSpeakerBtn = (speaker: Speaker) => {
      const isSelected = speaker.uuid === currentSpeakerUuid;
      const color = getSpeakerColor(speaker.name);
      // If the speaker has a human name but the underlying id is SPEAKER_##,
      // append the number so the user can see continuity in the ordered list.
      const slotNumber = placeholderSpeakerNumber(speaker.name);
      const effectiveName = translateSpeakerLabel(speaker.display_name || speaker.name);
      const label =
        slotNumber !== null && speaker.display_name
          ? `${effectiveName} (${slotNumber.toString().padStart(2, '0')})`
          : effectiveName;
      const speakerBtn = createDropdownItem(
        speaker.uuid,
        label,
        isSelected,
        color.bg,
        color.border
      );
      speakerBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        handleSpeakerSelect(speaker.uuid);
      });
      menu.appendChild(speakerBtn);
    };

    for (const speaker of namedSpeakers) renderSpeakerBtn(speaker);
    for (const speaker of autoSpeakers) renderSpeakerBtn(speaker);

    // Clear and append
    portalContainer.innerHTML = '';
    portalContainer.appendChild(menu);

    if (refocusInlineName && inlineInput) {
      inlineInput.focus();
      inlineInput.setSelectionRange(inlineInput.value.length, inlineInput.value.length);
    }
  }
</script>

<svelte:head>
  <style>
    .speaker-dropdown-portal .dropdown-menu {
      position: fixed;
      background: var(--surface-color, #1e293b);
      border: 1px solid var(--border-color, #334155);
      border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
      z-index: 10000;
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
      text-align: left;
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
      color: var(--primary-color, #3b82f6);
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
      color: var(--primary-color, #3b82f6);
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
      color: var(--text-color, #f1f5f9);
      background: var(--background-color, #0f172a);
      border: 1px solid var(--border-color, #334155);
      border-radius: 6px;
    }

    .speaker-dropdown-portal .inline-name-input:focus {
      outline: 2px solid var(--primary-color, #3b82f6);
      outline-offset: -1px;
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
    maxWidth="420px"
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
    font-weight: 700;
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
