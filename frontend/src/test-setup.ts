// Vitest global setup. Adds jest-dom matchers (toBeInTheDocument, etc.) to expect().
import '@testing-library/jest-dom/vitest';

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
