<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { getSpeakerColor } from '$lib/utils/speakerColors';
  import type { Speaker, Segment } from '$lib/types/speaker';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';

  export let segment: Segment;
  export let speakers: Speaker[] = [];
  export let mediaFileUuid: string = '';

  const dispatch = createEventDispatcher();
  let triggerButton: HTMLButtonElement;
  let portalContainer: HTMLDivElement | null = null;
  let isOpen = false;
  let isCreatingSpeaker = false;

  // Compute the next speaker name (e.g., SPEAKER_03 if SPEAKER_00, SPEAKER_01, SPEAKER_02 exist)
  function getNextSpeakerName(): string {
    let maxNumber = -1;
    for (const speaker of speakers) {
      const match = speaker.name?.match(/^SPEAKER_(\d+)$/);
      if (match) {
        const num = parseInt(match[1], 10);
        if (num > maxNumber) maxNumber = num;
      }
    }
    const nextNum = maxNumber + 1;
    return `SPEAKER_${nextNum.toString().padStart(2, '0')}`;
  }

  async function handleCreateNewSpeaker() {
    if (!mediaFileUuid || isCreatingSpeaker) return;

    isCreatingSpeaker = true;
    const newSpeakerName = getNextSpeakerName();

    try {
      const response = await axiosInstance.post(`/speakers?media_file_uuid=${mediaFileUuid}`, {
        name: newSpeakerName
      });

      const newSpeaker = response.data;

      // Dispatch event to notify parent that a new speaker was created
      dispatch('speakerCreated', { speaker: newSpeaker });

      // Auto-select the new speaker for this segment
      dispatch('change', {
        segmentUuid: segment.uuid,
        speakerUuid: newSpeaker.uuid
      });

      closeDropdown();
    } catch (error) {
      console.error('Failed to create new speaker:', error);
      toastStore.error($t('common.somethingWentWrong'));
    } finally {
      isCreatingSpeaker = false;
    }
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

    isOpen = !isOpen;

    if (isOpen) {
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
    const estimatedHeight = headerHeight + itemHeight + dividerHeight + (speakers.length * itemHeight) + (mediaFileUuid ? dividerHeight + itemHeight : 0);

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
      return;
    }

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

    // "Create New Speaker" button — top of list for quick access
    if (mediaFileUuid) {
      const nextSpeakerName = getNextSpeakerName();

      const createBtn = document.createElement('button');
      createBtn.className = 'dropdown-item create-speaker-btn';
      createBtn.dataset.action = 'create-speaker';

      const createOption = document.createElement('div');
      createOption.className = 'speaker-option';
      createOption.appendChild(createPlusSvg());

      const createSpan = document.createElement('span');
      createSpan.textContent = `${$t('speaker.createNew')} ${nextSpeakerName}`;
      createOption.appendChild(createSpan);

      createBtn.appendChild(createOption);
      createBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        handleCreateNewSpeaker();
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
    const isAutoLabel = (s: Speaker) =>
      /^SPEAKER_\d+$/.test(s.display_name || s.name);
    const namedSpeakers = speakers.filter((s) => !isAutoLabel(s));
    const autoSpeakers = speakers.filter((s) => isAutoLabel(s));

    const renderSpeakerBtn = (speaker: Speaker) => {
      const isSelected = speaker.uuid === currentSpeakerUuid;
      const color = getSpeakerColor(speaker.name);
      // If the speaker has a human name but the underlying id is SPEAKER_##,
      // append the number so the user can see continuity in the ordered list.
      const numMatch = speaker.name.match(/^SPEAKER_(\d+)$/);
      const effectiveName = translateSpeakerLabel(speaker.display_name || speaker.name);
      const label =
        numMatch && speaker.display_name ? `${effectiveName} (${numMatch[1]})` : effectiveName;
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

    .speaker-dropdown-portal .create-speaker-btn svg {
      color: var(--primary-color, #3b82f6);
      margin-right: 4px;
    }
  </style>
</svelte:head>

<div class="speaker-dropdown-container">
  <button
    class="speaker-trigger"
    bind:this={triggerButton}
    on:click={toggleDropdown}
    title={$t('speaker.clickToChangeSpeaker')}
  >
    <div
      class="segment-speaker"
      style="background-color: {getSpeakerColor(segment.speaker?.name || segment.speaker_label || 'Unknown').bg}; border-color: {getSpeakerColor(segment.speaker?.name || segment.speaker_label || 'Unknown').border}; --speaker-light: {getSpeakerColor(segment.speaker?.name || segment.speaker_label || 'Unknown').textLight}; --speaker-dark: {getSpeakerColor(segment.speaker?.name || segment.speaker_label || 'Unknown').textDark};"
    >
      {translateSpeakerLabel(segment.speaker?.display_name || segment.speaker?.name || segment.speaker_label || $t('common.unknown'))}
    </div>
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
  </button>
</div>

<style>
  .speaker-dropdown-container {
    position: relative;
    display: inline-block;
  }

  .speaker-trigger {
    display: flex;
    align-items: center;
    gap: 4px;
    background: none;
    border: none;
    cursor: pointer;
    padding: 0;
  }

  .segment-speaker {
    font-size: 12px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 12px;
    white-space: nowrap;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    border: 1px solid;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: all 0.2s ease;
    color: var(--speaker-light);
  }

  :global([data-theme='dark']) .segment-speaker {
    color: var(--speaker-dark);
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
</style>
