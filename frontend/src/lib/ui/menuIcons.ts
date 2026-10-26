/**
 * Glyphs for menu items and inline status icons, as data. One set so every dropdown, status
 * row and toast draws the same icon at the same size: Feather/Lucide geometry on a 24-unit
 * grid, 2px round strokes, `currentColor`, which is the convention the gallery selection menus
 * (Organize / Process) established. The app renders no emoji or text glyph as an icon
 * (`components/noEmoji.convention.test.ts`); add a shape here instead.
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
/** A solid dot: status and "unsaved changes" indicators. */
const dot = (r: number): IconShape => [
  'circle',
  { cx: '12', cy: '12', r: String(r), fill: 'currentColor', stroke: 'none' },
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
  // Markdown: the "M down-arrow" mark in a frame.
  'format-md': [rect(2, 5, 20, 14, 2), d('M6 15V9l3 3 3-3v6'), d('M17 9v6m-2.5-2.5L17 15l2.5-2.5')],
  // Word and PDF documents: the page glyph with a W and a P on it, so the two stay distinct.
  'format-docx': [
    d('M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'),
    poly('14 2 14 8 20 8'),
    d('M7.5 12l1.5 6 3-5 3 5 1.5-6'),
  ],
  'format-pdf': [
    d('M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'),
    poly('14 2 14 8 20 8'),
    d('M9 18v-6h2.2a1.9 1.9 0 0 1 0 3.8H9'),
  ],
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
  // Search result controls: pin the info panel, disclosure caret, filter chips, unpin.
  pin: [
    line(12, 17, 12, 22),
    d(
      'M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24z'
    ),
  ],
  'chevron-down': [poly('6 9 12 15 18 9')],
  tag: [
    d(
      'M12.59 2.59A2 2 0 0 0 11.17 2H4a2 2 0 0 0-2 2v7.17a2 2 0 0 0 .59 1.42l8.7 8.7a2.43 2.43 0 0 0 3.42 0l6.58-6.58a2.43 2.43 0 0 0 0-3.42z'
    ),
    circle(7.5, 7.5, 0.5),
  ],
  folder: [
    d(
      'M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z'
    ),
  ],
  close: [line(18, 6, 6, 18), line(6, 6, 18, 18)],
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
  // Status and inline glyphs (they replaced emoji and text symbols such as check marks,
  // crosses, warning signs, hourglasses and arrows).
  check: [poly('20 6 9 17 4 12')],
  x: [line(18, 6, 6, 18), line(6, 6, 18, 18)],
  'status-success': [circle(12, 12, 10), poly('8 12 11 15 16 9')],
  'status-error': [circle(12, 12, 10), line(15, 9, 9, 15), line(9, 9, 15, 15)],
  'status-cancelled': [circle(12, 12, 10), line(4.93, 4.93, 19.07, 19.07)],
  'status-queued': [circle(12, 12, 10), poly('12 6 12 12 15 15')],
  'status-working': [d('M21 12a9 9 0 1 1-6.22-8.56')],
  upload: [
    d('M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'),
    poly('17 8 12 3 7 8'),
    line(12, 3, 12, 15),
  ],
  retry: [poly('23 4 23 10 17 10'), d('M20.49 15a9 9 0 1 1-2.12-9.36L23 10')],
  warning: [
    d('M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'),
    line(12, 9, 12, 13),
    line(12, 17, 12.01, 17),
  ],
  info: [circle(12, 12, 10), line(12, 16, 12, 12), line(12, 8, 12.01, 8)],
  // Bare marks for a glyph drawn inside a filled status disc (toasts, issue rows).
  exclamation: [line(12, 5, 12, 14), line(12, 19, 12.01, 19)],
  'info-mark': [line(12, 10, 12, 19), line(12, 5, 12.01, 5)],
  // A shield: "this is protected" (a legal hold), distinct from the lock of a restricted collection.
  shield: [d('M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z')],
  lock: [rect(3, 11, 18, 11, 2), d('M7 11V7a5 5 0 0 1 10 0v4')],
  clipboard: [
    d('M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2'),
    rect(8, 2, 8, 4, 1),
  ],
  cpu: [
    rect(4, 4, 16, 16, 2),
    rect(9, 9, 6, 6),
    d('M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3'),
  ],
  plus: [line(12, 5, 12, 19), line(5, 12, 19, 12)],
  checkbox: [rect(3, 3, 18, 18, 2)],
  'circle-outline': [circle(12, 12, 7)],
  dot: [dot(6)],
  'dot-small': [dot(3.5)],
  // Directional: MenuIcon mirrors these in RTL (see RTL_MIRRORED below).
  'chevron-right': [poly('9 18 15 12 9 6')],
  'chevron-left': [poly('15 18 9 12 15 6')],
  'arrow-right': [line(5, 12, 19, 12), poly('12 5 19 12 12 19')],
  // View controls (the legal reader's full-screen toggle).
  maximize: [
    poly('15 3 21 3 21 9'),
    poly('9 21 3 21 3 15'),
    line(21, 3, 14, 10),
    line(3, 21, 10, 14),
  ],
  minimize: [
    poly('4 14 10 14 10 20'),
    poly('20 10 14 10 14 4'),
    line(14, 10, 21, 3),
    line(3, 21, 10, 14),
  ],
} as const satisfies Record<string, readonly IconShape[]>;

export type MenuIconName = keyof typeof MENU_ICONS;

/**
 * Glyphs that point along the reading direction: drawn for LTR and flipped by `MenuIcon` when
 * the document is RTL, so a caller cannot forget to. `chevron-down` is not directional.
 */
export const RTL_MIRRORED: ReadonlySet<MenuIconName> = new Set<MenuIconName>([
  'chevron-right',
  'chevron-left',
  'arrow-right',
]);

/** The `size` tokens (`--icon-size-*` in `styles/theme.css`) and their pixel fallbacks. */
export const ICON_SIZE_PX = { xs: 12, sm: 14, md: 16, lg: 20, xl: 32 } as const;
export type IconSizeToken = keyof typeof ICON_SIZE_PX;

/**
 * Task statuses (admin task health, system statistics) to the glyph their tables show in place
 * of the word on narrow screens. `null`: no glyph, show the (shortened) word.
 */
export function taskStatusIcon(status: string | null | undefined): MenuIconName | null {
  const icons: Record<string, MenuIconName> = {
    completed: 'status-success',
    success: 'status-success',
    processing: 'status-working',
    in_progress: 'status-working',
    pending: 'status-queued',
    error: 'status-error',
    failed: 'status-error',
  };
  return icons[(status ?? '').toLowerCase()] ?? null;
}

/** Sort-field values (gallery and search) to their glyph. */
export const SORT_ICON_BY_VALUE: Record<string, MenuIconName> = {
  upload_time: 'sort-date',
  completed_at: 'sort-completed',
  filename: 'sort-name',
  duration: 'sort-duration',
  file_size: 'sort-size',
  relevance: 'sort-relevance',
};
