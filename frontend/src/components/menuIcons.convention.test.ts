/**
 * Every actionable item in a dropdown menu carries an icon (issue #1243 item 13): the gallery
 * selection menus set the convention, and the transcript Export / Download menus and the
 * selection menu's own export entries were the holdouts, which read as unfinished next to them.
 *
 * Static, on purpose: a render test only sees the menus someone remembered to mount, while this
 * reads every component's markup, so a new `.dropdown-item` without a glyph fails here even if
 * no test ever opens that menu. Items built imperatively in script (SegmentSpeakerDropdown's
 * portal) are outside its reach and carry a speaker colour dot / glyph by construction.
 */
import { describe, it, expect } from 'vitest';
import { MENU_ICONS, SORT_ICON_BY_VALUE } from '$lib/ui/menuIcons';

const sources = import.meta.glob('/src/**/*.svelte', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** `<button|a ...>body</button|a>` whose opening tag matches `isMenuItem`. */
function menuItems(source: string, isMenuItem: (openTag: string) => boolean) {
  const found: { open: string; body: string }[] = [];
  // Attributes may hold `{...}` expressions (arrow functions contain `>`), nested up to two deep.
  const re = /<(button|a)\b((?:[^>{]|\{(?:[^{}]|\{[^{}]*\})*\})*)>([\s\S]*?)<\/\1>/g;
  for (const m of source.matchAll(re)) {
    if (isMenuItem(m[2])) found.push({ open: m[2].trim().replace(/\s+/g, ' '), body: m[3] });
  }
  return found;
}

const hasIcon = (body: string) => /<svg\b|<MenuIcon\b/.test(body);

const isDropdownItem = (open: string) => /class="[^"]*\bdropdown-item\b/.test(open);

describe('menu icon convention', () => {
  it('finds the menus it is meant to police (the scan is not vacuous)', () => {
    const withItems = Object.entries(sources).filter(
      ([, src]) => menuItems(src, isDropdownItem).length > 0
    );
    const names = withItems.map(([p]) => p.split('/').pop());
    expect(names).toEqual(
      expect.arrayContaining([
        'GallerySelectionActions.svelte',
        'UserDropdown.svelte',
        'SortDropdown.svelte',
      ])
    );
  });

  it('gives every .dropdown-item an icon', () => {
    const bare: string[] = [];
    for (const [path, src] of Object.entries(sources)) {
      for (const item of menuItems(src, isDropdownItem)) {
        if (!hasIcon(item.body)) bare.push(`${path.split('/').pop()}: <${item.open.slice(0, 70)}>`);
      }
    }
    expect(bare).toEqual([]);
  });

  it('gives every transcript Export / Download menu entry an icon', () => {
    const src = sources['/src/components/transcript/TranscriptActionsBar.svelte'];
    // Everything that is a <button> but not one of the three bar triggers is a menu entry.
    const entries = menuItems(
      src,
      (open) => !/export-transcript-button|edit-speakers-button|action-button/.test(open)
    );
    // 5 export formats + 2 conditional video entries + 3 audio entries.
    expect(entries.length).toBe(10);
    expect(entries.filter((e) => !hasIcon(e.body))).toEqual([]);
  });

  it('draws each export format with its own glyph', () => {
    const src = sources['/src/components/transcript/TranscriptActionsBar.svelte'];
    const used = [...src.matchAll(/<MenuIcon name="(format-[a-z]+)"/g)].map((m) => m[1]);
    expect(used.sort()).toEqual(
      ['format-csv', 'format-json', 'format-srt', 'format-txt', 'format-vtt'].sort()
    );
    const shapes = used.map((n) => JSON.stringify(MENU_ICONS[n as keyof typeof MENU_ICONS]));
    expect(new Set(shapes).size).toBe(used.length);
  });

  it('maps every sort field used by the gallery and search to a defined icon', () => {
    const fields = [
      'upload_time',
      'completed_at',
      'filename',
      'duration',
      'file_size',
      'relevance',
    ];
    for (const f of fields) expect(MENU_ICONS[SORT_ICON_BY_VALUE[f]].length).toBeGreaterThan(0);
  });
});
