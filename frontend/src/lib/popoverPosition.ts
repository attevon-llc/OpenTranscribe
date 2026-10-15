/**
 * Where to put a floating panel that is anchored to a trigger and drawn on a
 * `position: fixed` layer (so no scrolling ancestor can clip it).
 *
 * Pure geometry, kept out of the component so the flip/clamp rules can be tested
 * without a layout engine (jsdom has none).
 */
export interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

export interface PopoverPlacement {
  /** Fixed-position offsets in CSS px. */
  top: number;
  left: number;
  /** Width the panel should be given. */
  width: number;
  /** Height cap so the panel scrolls internally instead of leaving the viewport. */
  maxHeight: number;
  /** Which side of the trigger it landed on. */
  side: 'below' | 'above';
  /** Narrow viewport: render as a bottom sheet instead of anchoring. */
  sheet: boolean;
}

export interface PlacementOptions {
  /** Gap between trigger and panel. */
  gap?: number;
  /** Minimum distance kept from every viewport edge. */
  margin?: number;
  /** Panel is never narrower than this (when the viewport allows). */
  minWidth?: number;
  /** Viewports at or below this width get a bottom sheet. */
  sheetBreakpoint?: number;
  /** Right-to-left layout: align the panel to the trigger's right edge. */
  rtl?: boolean;
  /** Exact panel width (e.g. a menu's own content width) instead of trigger-width/minWidth. */
  width?: number;
  /** Physical edge of the trigger the panel aligns to; defaults to the inline-start edge. */
  edge?: 'left' | 'right';
}

export function computePopoverPlacement(
  trigger: Box,
  panelHeight: number,
  viewport: { width: number; height: number },
  options: PlacementOptions = {}
): PopoverPlacement {
  const { gap = 6, margin = 8, minWidth = 300, sheetBreakpoint = 560, rtl = false, edge } = options;

  if (viewport.width <= sheetBreakpoint) {
    return {
      top: 0,
      left: 0,
      width: viewport.width,
      maxHeight: Math.round(viewport.height * 0.85),
      side: 'above',
      sheet: true,
    };
  }

  const wanted = options.width ?? Math.max(trigger.width, minWidth);
  const width = Math.min(wanted, viewport.width - margin * 2);

  // Horizontal: align the panel's inline-start edge with the trigger's, then clamp.
  const alignRight = edge ? edge === 'right' : rtl;
  const aligned = alignRight ? trigger.left + trigger.width - width : trigger.left;
  const left = Math.round(Math.min(Math.max(aligned, margin), viewport.width - width - margin));

  const roomBelow = viewport.height - (trigger.top + trigger.height) - gap - margin;
  const roomAbove = trigger.top - gap - margin;
  // Prefer below; flip only when it does not fit there AND there is more room above.
  const side: 'below' | 'above' =
    panelHeight > roomBelow && roomAbove > roomBelow ? 'above' : 'below';
  const room = side === 'below' ? roomBelow : roomAbove;
  const maxHeight = Math.max(room, 0);
  const height = Math.min(panelHeight, maxHeight);
  const top = side === 'below' ? trigger.top + trigger.height + gap : trigger.top - gap - height;

  return {
    top: Math.round(Math.max(top, margin)),
    left,
    width,
    maxHeight: Math.floor(maxHeight),
    side,
    sheet: false,
  };
}
