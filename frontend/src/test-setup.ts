// Vitest global setup. Adds jest-dom matchers (toBeInTheDocument, etc.) to expect().
import '@testing-library/jest-dom/vitest';
import { configure } from '@testing-library/svelte/pure';

// Budget for `waitFor` / `findBy*`. Several components reach their final DOM only after a
// lazy `import()` (the DOMPurify sanitizer in LegalDocumentViewer, KaTeX in ChatMarkdown,
// the About modal in Navbar), and the first import of a module in a worker includes its
// Vite transform, queued on the single main vitest thread behind every other worker's
// requests. Testing Library's 1 s default was calibrated for a re-render, not a transform:
// measured 2026-10-22 on the 48-core shared host at load ~120, the
// LegalDocumentViewer sanitized-markdown test took 2.71 s end to end and its 1 s waitFor
// failed; at load ~50 with the vitest.config.ts worker cap it took 0.58 s. 3 s covers the
// measured worst case and stays below the 5 s test timeout, so a real failure still
// reports the waitFor's own assertion message, not an anonymous test timeout. Only a
// failing wait is slower; a passing one returns as soon as its assertion holds.
const ASYNC_UTIL_TIMEOUT_MS = 3_000;
configure({ asyncUtilTimeout: ASYNC_UTIL_TIMEOUT_MS });

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
// engine). TranscriptViewModal's (formerly SearchTranscriptModal's) jump-to-match
// calls it directly, which throws `TypeError: ... is not a function` and
// previously only escaped notice because no test drove a paging retry far
// enough to actually resolve a match under fake timers.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}
