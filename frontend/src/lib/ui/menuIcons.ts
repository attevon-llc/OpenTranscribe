/**
 * Glyphs for menu items, as data. One set so every dropdown draws the same icon at the same
 * size: Feather/Lucide geometry on a 24-unit grid, 2px round strokes, `currentColor`, which is
 * the convention the gallery selection menus (Organize / Process) established.
 *
 * Shapes are tuples rather than markup strings so `MenuIcon.svelte` renders them with
 * `<svelte:element>` instead of `{@html}`.
 */
export type IconShape = readonly [
  tag: 'path' | 'polyline' | 'line' | 'rect' | 'circle',
  attrs: Record<string, string>,
];

const d = (value: string): IconShape => ['path', { d: value }];
const poly = (points: string): IconShape => ['polyline', { points }];
const line = (x1: number, y1: number, x2: number, y2: number): IconShape => [
  'line',
  { x1: String(x1), y1: String(y1), x2: String(x2), y2: String(y2) },
];
const circle = (cx: number, cy: number, r: number): IconShape => [
  'circle',
  { cx: String(cx), cy: String(cy), r: String(r) },
];
const rect = (x: number, y: number, width: number, height: number, rx = 0): IconShape => [
  'rect',
  {
    x: String(x),
    y: String(y),
    width: String(width),
    height: String(height),
    ...(rx ? { rx: String(rx) } : {}),
  },
];

export const MENU_ICONS = {
  // Transcript formats: one distinct glyph each, so a list of formats is scannable.
  'format-txt': [
    d('M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'),
    poly('14 2 14 8 20 8'),
    line(16, 13, 8, 13),
    line(16, 17, 8, 17),
    poly('10 9 9 9 8 9'),
  ],
  'format-json': [poly('16 18 22 12 16 6'), poly('8 6 2 12 8 18')],
  'format-csv': [
    rect(3, 3, 18, 18, 2),
    line(3, 9, 21, 9),
    line(3, 15, 21, 15),
    line(12, 3, 12, 21),
  ],
  'format-srt': [rect(3, 5, 18, 14, 2), d('M7 15h4M15 15h2M7 11h2M13 11h4')],
  'format-vtt': [poly('23 7 16 12 23 17 23 7'), rect(1, 5, 15, 14, 2)],
  // The Export trigger: a document with an arrow out of it, so it never reads as the Download button.
  'export-file': [
    d('M14 22h4a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v4'),
    d('M14 2v4a2 2 0 0 0 2 2h4'),
    d('M2 15h10'),
    poly('9 18 12 15 9 12'),
  ],
  // Download targets.
  download: [
    d('M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'),
    poly('7 10 12 15 17 10'),
    line(12, 15, 12, 3),
  ],
  'video-original': [
    rect(2, 2, 20, 20, 2.18),
    line(7, 2, 7, 22),
    line(17, 2, 17, 22),
    line(2, 12, 22, 12),
    line(2, 7, 7, 7),
    line(2, 17, 7, 17),
    line(17, 17, 22, 17),
    line(17, 7, 22, 7),
  ],
  'video-subtitles': [rect(2, 4, 20, 16, 2), d('M6 15h4M14 15h4M6 11h3M12 11h6')],
  'audio-mp3': [d('M9 18V5l12-2v13'), circle(6, 18, 3), circle(18, 16, 3)],
  'audio-wav': [d('M2 10v3M6 6v11M10 3v18M14 8v7M18 5v13M22 10v3')],
  'audio-original': [
    d('M3 18v-6a9 9 0 0 1 18 0v6'),
    d(
      'M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z'
    ),
  ],
  // Sort fields.
  'sort-date': [rect(3, 4, 18, 18, 2), line(16, 2, 16, 6), line(8, 2, 8, 6), line(3, 10, 21, 10)],
  'sort-completed': [
    rect(3, 4, 18, 18, 2),
    line(16, 2, 16, 6),
    line(8, 2, 8, 6),
    line(3, 10, 21, 10),
    poly('9 16 11 18 15 14'),
  ],
  'sort-name': [poly('4 7 4 4 20 4 20 7'), line(9, 20, 15, 20), line(12, 4, 12, 20)],
  'sort-duration': [circle(12, 12, 10), poly('12 6 12 12 16 14')],
  'sort-size': [
    line(22, 12, 2, 12),
    d(
      'M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z'
    ),
    line(6, 16, 6.01, 16),
    line(10, 16, 10.01, 16),
  ],
  'sort-relevance': [circle(12, 12, 10), circle(12, 12, 6), circle(12, 12, 2)],
} as const satisfies Record<string, readonly IconShape[]>;

export type MenuIconName = keyof typeof MENU_ICONS;

/** Sort-field values (gallery and search) to their glyph. */
export const SORT_ICON_BY_VALUE: Record<string, MenuIconName> = {
  upload_time: 'sort-date',
  completed_at: 'sort-completed',
  filename: 'sort-name',
  duration: 'sort-duration',
  file_size: 'sort-size',
  relevance: 'sort-relevance',
};
