/**
 * DOM builders for the per-segment speaker menu.
 *
 * The open menu is built imperatively into a `document.body` portal (so no ancestor's
 * `overflow: hidden` clips it) and every string goes in through `textContent` or an
 * attribute, never `innerHTML`, so a speaker name cannot inject markup. The styling lives in
 * `SegmentSpeakerDropdown.svelte`'s `<svelte:head>` block under `.speaker-dropdown-portal`.
 *
 * This module owns the markup only. State (what is typed, which menu is open) and every
 * decision stay in the component, which hands in plain values and callbacks.
 */
import type { Speaker } from '$lib/types/speaker';
import type { Translate } from '$lib/i18n';
import { getSpeakerColor } from '$lib/utils/speakerColors';
import type { PendingSuggestion } from './speakerMenu';
import { partitionSpeakers, speakerMenuLabel } from './speakerMenu';

const SVG_NS = 'http://www.w3.org/2000/svg';

function svgRoot(size: string, fill: string): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', fill);
  return svg;
}

function strokeSvg(size: string): SVGSVGElement {
  const svg = svgRoot(size, 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  return svg;
}

export function createCheckmarkSvg(): SVGSVGElement {
  const svg = strokeSvg('16');
  const polyline = document.createElementNS(SVG_NS, 'polyline');
  polyline.setAttribute('points', '20 6 9 17 4 12');
  svg.appendChild(polyline);
  return svg;
}

function createPlusSvg(): SVGSVGElement {
  const svg = strokeSvg('14');
  const line1 = document.createElementNS(SVG_NS, 'line');
  line1.setAttribute('x1', '12');
  line1.setAttribute('y1', '5');
  line1.setAttribute('x2', '12');
  line1.setAttribute('y2', '19');
  const line2 = document.createElementNS(SVG_NS, 'line');
  line2.setAttribute('x1', '5');
  line2.setAttribute('y1', '12');
  line2.setAttribute('x2', '19');
  line2.setAttribute('y2', '12');
  svg.appendChild(line1);
  svg.appendChild(line2);
  return svg;
}

/** The "AI suggestion" sparkle. */
function createSparkleSvg(): SVGSVGElement {
  const svg = svgRoot('12', 'currentColor');
  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute(
    'd',
    'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z'
  );
  svg.appendChild(path);
  return svg;
}

/** A speaker row: colour dot, label, and a checkmark on the selected one. */
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

/**
 * Fill the inline field's profile-name suggestions in place. Rebuilding the field's own row
 * on each keystroke would drop focus and abort an IME composition, so only this list changes.
 */
export function renderProfileMatches(
  container: HTMLElement,
  matches: string[],
  labelText: string,
  onPick: (name: string) => void
): void {
  container.replaceChildren();
  container.hidden = matches.length === 0;
  if (matches.length === 0) return;

  const label = document.createElement('div');
  label.className = 'dropdown-header inline-name-matches-label';
  label.textContent = labelText;
  container.appendChild(label);
  for (const name of matches) {
    const pick = document.createElement('button');
    pick.className = 'dropdown-item inline-name-match';
    pick.dataset.action = 'pick-profile';
    pick.textContent = name; // textContent is XSS-safe
    pick.addEventListener('click', (e) => {
      e.stopPropagation();
      onPick(name);
    });
    container.appendChild(pick);
  }
}

export interface SpeakerMenuInput {
  speakers: Speaker[];
  /** uuid of the speaker currently on the segment; undefined when it has none. */
  currentSpeakerUuid: string | undefined;
  /** The segment has no speaker at all (checks the "No Speaker" row). */
  noSpeaker: boolean;
  /** The segment's speaker uuid; the inline name field only exists for a segment that has one. */
  wireSpeakerUuid: string | undefined;
  speakerNumberLabel: string;
  inlineName: string;
  pendingSuggestion: PendingSuggestion | null;
  /** Show "Add speaker" (needs a file to create the speaker in). */
  canCreateSpeaker: boolean;
  t: Translate;
  translateLabel: (name: string) => string;
  onInlineInput: (value: string) => void;
  onSubmitInline: () => void;
  onEscape: () => void;
  onAcceptSuggestion: () => void;
  onCreateSpeaker: () => void;
  onSelect: (speakerUuid: string | null) => void;
}

export interface SpeakerMenuParts {
  menu: HTMLDivElement;
  /** The inline name field and the nodes the component updates in place while typing. */
  inlineInput: HTMLInputElement | null;
  inlineSaveButton: HTMLButtonElement | null;
  inlineHint: HTMLElement | null;
  inlineMatches: HTMLElement | null;
}

function divider(): HTMLDivElement {
  const el = document.createElement('div');
  el.className = 'dropdown-divider';
  return el;
}

function buildInlineNameRow(menu: HTMLElement, input: SpeakerMenuInput) {
  const row = document.createElement('div');
  row.className = 'inline-name-row';

  const field = document.createElement('input');
  field.type = 'text';
  field.className = 'inline-name-input';
  field.maxLength = 255;
  field.autocomplete = 'off';
  // A name typed in Arabic must align right even inside an LTR page, and vice versa.
  field.dir = 'auto';
  field.placeholder = input.t('speaker.nameThisSpeakerPlaceholder', {
    label: input.translateLabel(input.speakerNumberLabel || input.t('common.unknown')),
  });
  field.setAttribute('aria-label', input.t('speaker.nameThisSpeakerLabel'));
  field.value = input.inlineName;
  field.addEventListener('input', () => input.onInlineInput(field.value));
  field.addEventListener('keydown', (e) => {
    // Typing must not reach the page's own shortcuts (play/pause, seek, ...).
    e.stopPropagation();
    if (e.key === 'Enter') {
      e.preventDefault();
      input.onSubmitInline();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      input.onEscape();
    }
  });
  row.appendChild(field);

  const save = document.createElement('button');
  save.className = 'inline-name-save';
  save.dataset.action = 'save-inline-name';
  save.setAttribute('aria-label', input.t('speaker.nameThisSpeakerSave'));
  save.title = input.t('speaker.nameThisSpeakerSave');
  save.appendChild(createCheckmarkSvg());
  save.addEventListener('click', (e) => {
    e.stopPropagation();
    input.onSubmitInline();
  });
  row.appendChild(save);
  menu.appendChild(row);

  const hint = document.createElement('div');
  hint.className = 'inline-name-hint';
  hint.hidden = true;
  menu.appendChild(hint);

  const matchList = document.createElement('div');
  matchList.className = 'inline-name-matches';
  matchList.hidden = true;
  menu.appendChild(matchList);

  menu.appendChild(divider());
  return { field, save, hint, matchList };
}

function buildSuggestionRow(menu: HTMLElement, input: SpeakerMenuInput, s: PendingSuggestion) {
  const suggestionHeader = document.createElement('div');
  suggestionHeader.className = 'dropdown-header suggestion-header';
  suggestionHeader.textContent = input.t('speaker.unconfirmedSuggestion');
  menu.appendChild(suggestionHeader);

  const suggestionBtn = document.createElement('button');
  suggestionBtn.className = 'dropdown-item suggestion-btn';
  suggestionBtn.dataset.action = 'accept-suggestion';
  suggestionBtn.title = input.t('speaker.acceptSuggestionTitle');

  const suggestionOption = document.createElement('div');
  suggestionOption.className = 'speaker-option';
  suggestionOption.appendChild(createSparkleSvg());

  const suggestionSpan = document.createElement('span');
  suggestionSpan.textContent = s.name;
  suggestionOption.appendChild(suggestionSpan);
  suggestionBtn.appendChild(suggestionOption);

  const confidenceBadge = document.createElement('span');
  confidenceBadge.className = 'suggestion-confidence';
  confidenceBadge.textContent = `${Math.round(s.confidence * 100)}%`;
  suggestionBtn.appendChild(confidenceBadge);

  suggestionBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    input.onAcceptSuggestion();
  });
  menu.appendChild(suggestionBtn);
  menu.appendChild(divider());
}

function buildCreateSpeakerRow(menu: HTMLElement, input: SpeakerMenuInput) {
  const createBtn = document.createElement('button');
  createBtn.className = 'dropdown-item create-speaker-btn';
  createBtn.dataset.action = 'create-speaker';

  const createOption = document.createElement('div');
  createOption.className = 'speaker-option';
  createOption.appendChild(createPlusSvg());

  const createSpan = document.createElement('span');
  createSpan.textContent = input.t('speaker.newSpeakerForSegment');
  createOption.appendChild(createSpan);

  createBtn.appendChild(createOption);
  createBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    input.onCreateSpeaker();
  });
  menu.appendChild(createBtn);
}

/** Build the whole menu: name field, suggestion, "Add speaker", "No Speaker", then speakers. */
export function buildSpeakerMenu(input: SpeakerMenuInput): SpeakerMenuParts {
  const menu = document.createElement('div');
  menu.className = 'dropdown-menu';

  const header = document.createElement('div');
  header.className = 'dropdown-header';
  header.textContent = input.t('speaker.assignSpeaker');
  menu.appendChild(header);

  // Name THIS speaker (#1086): a field first, because it is the common action.
  let inline: ReturnType<typeof buildInlineNameRow> | null = null;
  if (input.wireSpeakerUuid) inline = buildInlineNameRow(menu, input);

  // Unconfirmed LLM / voice-match suggestion: offered HERE, with its confidence score, and
  // never in the name slot (#741). One affirmative click accepts it.
  if (input.pendingSuggestion) buildSuggestionRow(menu, input, input.pendingSuggestion);

  // "Add speaker" at the top of the list for quick access. Opens the naming dialog; the
  // speaker is not created until the user has typed a name (#740).
  if (input.canCreateSpeaker) buildCreateSpeakerRow(menu, input);

  const noSpeakerBtn = createDropdownItem(
    '',
    input.t('speaker.noSpeaker'),
    input.noSpeaker,
    undefined,
    undefined,
    true
  );
  noSpeakerBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    input.onSelect(null);
  });
  menu.appendChild(noSpeakerBtn);

  // Separates utility actions from named speakers.
  menu.appendChild(divider());

  const { named, auto } = partitionSpeakers(input.speakers);
  for (const speaker of [...named, ...auto]) {
    const color = getSpeakerColor(speaker.name);
    const speakerBtn = createDropdownItem(
      speaker.uuid,
      speakerMenuLabel(speaker, input.translateLabel),
      speaker.uuid === input.currentSpeakerUuid,
      color.bg,
      color.border
    );
    speakerBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      input.onSelect(speaker.uuid);
    });
    menu.appendChild(speakerBtn);
  }

  return {
    menu,
    inlineInput: inline?.field ?? null,
    inlineSaveButton: inline?.save ?? null,
    inlineHint: inline?.hint ?? null,
    inlineMatches: inline?.matchList ?? null,
  };
}
