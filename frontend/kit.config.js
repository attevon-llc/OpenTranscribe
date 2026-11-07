import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/**
 * SvelteKit 3 no longer reads `svelte.config.js`: this object is the argument to the
 * `sveltekit()` plugin in vite.config.ts (migration guide, "svelte.config.js no longer
 * supported"). It is a standalone module so eslint and csp.test.ts can import it too.
 *
 * @type {import('@sveltejs/kit').Config}
 */
export const kitConfig = {
  preprocess: vitePreprocess(),

  // Use static adapter for SPA mode
  adapter: adapter({
    pages: 'dist',
    assets: 'dist',
    fallback: 'index.html',
    precompress: false,
    strict: true,
  }),

  // SPA mode configuration
  prerender: {
    handleHttpError: 'warn',
    handleMissingId: 'warn',
  },

  // Content Security Policy — hash mode (adapter-static can't do per-request nonces).
  // SvelteKit hashes its own inline SPA bootstrap so we can drop `script-src 'unsafe-inline'`.
  // Emitted as a <meta http-equiv> in the built index.html. `frame-ancestors` is intentionally
  // omitted (invalid in a <meta> CSP — nginx's X-Frame-Options: SAMEORIGIN covers clickjacking).
  // `wasm-unsafe-eval` is required by the ffmpeg.wasm worker; `style-src 'unsafe-inline'` stays
  // (Svelte scoped styles + the app.html font/base <style> blocks).
  // `connect-src` is 'self' only: CSP Level 3 matches ws:/wss: to the page's own host under
  // 'self', which is all the notifications socket needs (its URL is built from
  // window.location). Bare `ws:`/`wss:` would allow a socket to ANY host — an exfiltration
  // path for injected script — and `ws:` a plaintext one (issue #1028).
  csp: {
    mode: 'hash',
    directives: {
      'default-src': ['self'],
      'script-src': ['self', 'wasm-unsafe-eval'],
      'style-src': ['self', 'unsafe-inline'],
      'img-src': ['self', 'data:', 'blob:'],
      'font-src': ['self', 'data:'],
      'connect-src': ['self'],
      'media-src': ['self', 'blob:'],
      'worker-src': ['self', 'blob:'],
      'object-src': ['none'],
      'base-uri': ['self'],
      'form-action': ['self'],
    },
  },

  // Deprecated in Kit 3 in favour of package.json `imports` (#lib); `$lib` is no longer generated
  // automatically, so it is declared here to keep the existing `$lib/...` imports resolving.
  alias: {
    $lib: './src/lib',
    $components: './src/components',
    $stores: './src/stores',
  },
};
