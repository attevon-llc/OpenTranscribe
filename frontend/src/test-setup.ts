// Vitest global setup. Adds jest-dom matchers (toBeInTheDocument, etc.) to expect().
import '@testing-library/jest-dom/vitest';

// jsdom doesn't implement `window.matchMedia`. Svelte 5's `MediaQuery` (used by
// `svelte/motion` via `prefers-reduced-motion`) calls it at MODULE EVALUATION
// time, so a component that merely imports `svelte-range-slider-pips` throws
// before its first line runs — which is what kept SettingsModal, search/+page
// and +layout.svelte unloadable even after their `$app/*` imports resolved.
// `theme.js` also reads `prefers-color-scheme` here; tests that care override
// this with their own stub.
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// jsdom doesn't implement the Web Animations API that Svelte transitions
// (slide/fade/etc.) call via element.animate(). Provide a no-op stub so
// transition-using components can be rendered in unit tests.
if (typeof Element !== 'undefined' && !Element.prototype.animate) {
  Element.prototype.animate = function animate() {
    return {
      cancel() {},
      finish() {},
      play() {},
      pause() {},
      onfinish: null,
      oncancel: null,
      currentTime: 0,
      finished: Promise.resolve(),
    } as unknown as Animation;
  };
}

// jsdom doesn't implement Element.scrollIntoView (it has no real layout/scroll
// engine). SearchTranscriptModal's jump-to-match calls it directly, which
// throws `TypeError: ... is not a function` and previously only escaped
// notice because no test drove a paging retry far enough to actually resolve
// a match under fake timers.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}
