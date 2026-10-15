import { computePopoverPlacement } from '$lib/popoverPosition';

export interface AnchoredPanelOptions {
  /** The trigger (or field) the panel hangs off. Nothing is placed until it is set. */
  anchor: HTMLElement | null;
  /** `anchor` (default): as wide as the anchor. `content`: the panel's own natural width. */
  width?: 'anchor' | 'content';
  /** Never narrower than this when `width` is `anchor` (px). */
  minWidth?: number;
  /** Defaults to `document.documentElement.dir === 'rtl'`. */
  rtl?: boolean;
  /** Physical edge of the anchor to align to; default is the inline-start edge. */
  edge?: 'left' | 'right';
  /** Upper bound for the panel's height (px), for panels whose stylesheet capped them. */
  maxHeight?: number;
  /** Move the node to `<body>` (default). Pass false when the caller already hosts it there. */
  portal?: boolean;
  /** CSS z-index; defaults to just above modals. */
  zIndex?: string;
  /** Gap between anchor and panel (px). */
  gap?: number;
  /** Distance kept from every viewport edge (px). */
  margin?: number;
}

/**
 * Floating-panel placement for every dropdown, menu, popover and suggestion list.
 *
 * Moves the node to `<body>` and draws it on a `position: fixed` layer, so no scrolling
 * ancestor (the filter sidebar, a modal body) can clip it and no transformed ancestor (a
 * sliding drawer, an animated modal) can re-root `fixed`. It opens below the anchor, flips
 * above when it does not fit below and there is more room above, caps `max-height` to the room
 * on the chosen side (the panel scrolls internally), and clamps horizontally, RTL included.
 * The chosen side is exposed as `data-side="above|below"` for styling.
 *
 * It re-places on window resize, scroll of ANY ancestor (capture) and panel size changes
 * (the searchable lists shrink while typing). The panel must scroll itself
 * (`overflow-y: auto`, or a flex column whose list has `min-height: 0; overflow-y: auto`).
 *
 * Because the node lives on `<body>`, a click-outside check must `ignore` it.
 */
export function anchoredPanel(node: HTMLElement, initial: AnchoredPanelOptions) {
  let options = initial;
  if (options.portal !== false) document.body.appendChild(node);
  node.style.position = 'fixed';
  // Above modals (--z-modal) like DateRangePicker's layer: a panel opened from inside a dialog
  // is now a body child, so it competes with the dialog's own stacking layer.
  node.style.zIndex = options.zIndex ?? 'calc(var(--z-modal, 1300) + 10)';

  function place() {
    const anchor = options.anchor;
    if (!anchor || !node.isConnected) return;
    const content = options.width === 'content';

    // Measure the natural size: lift our own caps first, or a shrunk panel can never grow back.
    // Width goes back to the stylesheet's (or shrink-to-fit from the viewport's left edge).
    node.style.maxHeight = 'none';
    node.style.width = '';
    node.style.left = '0px';
    node.style.top = '0px';
    const natural = node.getBoundingClientRect();
    const ceiling = options.maxHeight ?? Infinity;

    const a = anchor.getBoundingClientRect();
    const rtl = options.rtl ?? document.documentElement.dir === 'rtl';
    const p = computePopoverPlacement(
      { top: a.top, left: a.left, width: a.width, height: a.height },
      Math.min(natural.height, ceiling),
      { width: window.innerWidth, height: window.innerHeight },
      {
        gap: options.gap ?? 6,
        margin: options.margin ?? 8,
        minWidth: options.minWidth ?? 0,
        sheetBreakpoint: 0,
        rtl,
        edge: options.edge,
        width: content ? natural.width : undefined,
      }
    );

    node.style.top = `${p.top}px`;
    node.style.left = `${p.left}px`;
    node.style.width = `${p.width}px`;
    node.style.maxHeight = `${Math.min(p.maxHeight, ceiling)}px`;
    node.dataset.side = p.side;
  }

  const onScroll = (event: Event) => {
    // The panel's own list scrolling must not trigger a re-measure.
    if (event.target instanceof Node && node.contains(event.target)) return;
    place();
  };

  window.addEventListener('resize', place);
  document.addEventListener('scroll', onScroll, true);
  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place);
  observer?.observe(node);
  place();

  return {
    update(next: AnchoredPanelOptions) {
      options = next;
      place();
    },
    destroy() {
      window.removeEventListener('resize', place);
      document.removeEventListener('scroll', onScroll, true);
      observer?.disconnect();
      // Moved out of its Svelte-managed parent, so Svelte will not remove it on teardown.
      node.remove();
    },
  };
}
