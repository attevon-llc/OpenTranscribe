/**
 * Track the two widths the knowledge panel needs (~44rem column + 2rem gap + 19rem panel of
 * CONTENT width; the filter sidebar takes 320px open / 50px collapsed, see
 * docs/design/search_results_ux_audit.md). Calls `onChange` now and on every change; returns the
 * cleanup.
 */
export function watchPanelViewport(
  onChange: (viewport: { wide: boolean; mid: boolean }) => void
): () => void {
  const wideMq = window.matchMedia('(min-width: 1440px)');
  const midMq = window.matchMedia('(min-width: 1180px)');
  const sync = () => onChange({ wide: wideMq.matches, mid: midMq.matches });
  sync();
  wideMq.addEventListener('change', sync);
  midMq.addEventListener('change', sync);
  return () => {
    wideMq.removeEventListener('change', sync);
    midMq.removeEventListener('change', sync);
  };
}
