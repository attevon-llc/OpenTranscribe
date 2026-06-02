import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  // Consult https://svelte.dev/docs/kit/integrations
  // for more information about preprocessors
  preprocess: vitePreprocess(),

  kit: {
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
    csp: {
      mode: 'hash',
      directives: {
        'default-src': ['self'],
        'script-src': ['self', 'wasm-unsafe-eval'],
        'style-src': ['self', 'unsafe-inline'],
        'img-src': ['self', 'data:', 'blob:'],
        'font-src': ['self', 'data:'],
        'connect-src': ['self', 'ws:', 'wss:'],
        'media-src': ['self', 'blob:'],
        'worker-src': ['self', 'blob:'],
        'object-src': ['none'],
        'base-uri': ['self'],
        'form-action': ['self'],
      },
    },

    // Alias configuration (matches vite.config.ts)
    alias: {
      $lib: './src/lib',
      $components: './src/components',
      $stores: './src/stores',
    },
  },
};

export default config;
