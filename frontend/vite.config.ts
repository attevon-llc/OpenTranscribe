import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, loadEnv } from 'vite';
import { fileURLToPath } from 'url';
import { createHash } from 'crypto';
import { readFileSync } from 'fs';
import path, { dirname } from 'path';
import { visualizer } from 'rollup-plugin-visualizer';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const pkg = JSON.parse(readFileSync(path.resolve(__dirname, 'package.json'), 'utf-8'));

/**
 * `static/theme.js` is the render-blocking theme bootstrap. It is deliberately an
 * external file (so the CSP can omit `script-src 'unsafe-inline'`), which also means
 * Vite never hashes it: nginx serves `/theme.js` with `expires 1y`, so an unversioned
 * URL can pin a stale copy in a browser cache indefinitely. Hash its contents and
 * append the digest as a cache-busting query string in `app.html`, giving it the same
 * change-exactly-when-the-file-changes behaviour as a hashed bundle asset.
 */
const themeVersion = createHash('sha256')
  .update(readFileSync(path.resolve(__dirname, 'static/theme.js')))
  .digest('hex')
  .slice(0, 8);

// Consumed by `%sveltekit.env.PUBLIC_THEME_VERSION%` in src/app.html. SvelteKit only
// substitutes vars carrying the public prefix, so this can never leak a private value.
process.env.PUBLIC_THEME_VERSION = themeVersion;

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // Load env file based on `mode` in the current directory.
  // Set the third parameter to '' to load all env regardless of the `VITE_` prefix.
  const env = loadEnv(mode, process.cwd(), '');

  // Bundle-size analysis is opt-in (ANALYZE=true or `--mode analyze`) so normal
  // builds are completely unaffected. Emits dist/stats.html after the build.
  const analyze = process.env.ANALYZE === 'true' || mode === 'analyze';

  return {
    plugins: [
      sveltekit(),
      ...(analyze
        ? [
            // adapter-static empties dist/ during finalization (after the Vite
            // sub-builds run), so writing straight to dist/stats.html gets wiped.
            // Emit to a staging dir; build:analyze copies it into dist/ afterward.
            visualizer({
              filename: path.resolve(__dirname, '.bundle-stats/stats.html'),
              gzipSize: true,
              brotliSize: true,
              template: 'treemap',
              emitFile: false,
            }),
          ]
        : []),
    ],
    server: {
      port: 5173,
      proxy: {
        '/health': {
          target: 'http://backend:8080',
          changeOrigin: true,
        },
        '/api': {
          target: 'http://backend:8080',
          changeOrigin: true,
          rewrite: (path) => path,
          secure: false,
          // Rewrite Location headers on 3xx redirects to use the proxy host
          // instead of the Docker-internal target host (backend:8080).
          // This is the http-proxy equivalent of nginx's `proxy_redirect default`
          // and ensures redirects work correctly when accessed via LAN IP or localhost.
          autoRewrite: true,
          configure: (proxy, _options) => {
            proxy.on('error', (err, _req, _res) => {
              console.log('proxy error', err);
            });
            proxy.on('proxyReq', (proxyReq, req, _res) => {
              console.log('Sending Request:', req.method, req.url);
            });
            proxy.on('proxyRes', (proxyRes, req, _res) => {
              console.log('Received Response from:', req.url, proxyRes.statusCode);
            });
          },
        },
        '/api/ws': {
          target: 'ws://backend:8080',
          ws: true,
          changeOrigin: true,
          rewrite: (path) => path,
        },
        // MinIO proxy for presigned URLs (secure media streaming)
        // Use VITE_MINIO_URL env var for remote dev (e.g., Mac -> Linux server)
        // Default: http://minio:9000 (Docker internal, works when frontend runs in Docker)
        '/minio': {
          target: env.VITE_MINIO_URL || 'http://minio:9000',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/minio/, ''),
          // Set Host header to match what MinIO signed the URL with
          headers: {
            Host: 'minio:9000',
          },
        },
        // Flower proxy for dev mode - injects Basic Auth header
        // so browsers don't need to handle credentials in URL (blocked by modern browsers)
        '/flower': {
          target: `http://flower:5555`,
          changeOrigin: true,
          headers: {
            Authorization: `Basic ${Buffer.from(
              `${env.FLOWER_USER || 'admin'}:${env.FLOWER_PASSWORD || 'flower'}`
            ).toString('base64')}`,
          },
        },
        // Embedded docs proxy - mirrors the nginx /docs/ location block
        // Allows offline access to documentation without leaving the app
        '/docs': {
          target: 'http://docs:8080',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/docs/, ''),
        },
        // S3 proxy for presigned URLs (thumbnails, media files)
        // Mirrors the nginx /s3/ location block for dev mode parity
        '/s3': {
          target: env.VITE_MINIO_URL || 'http://minio:9000',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/s3/, ''),
          headers: {
            Host: 'minio:9000',
          },
        },
      },
      // Add historyApiFallback to handle client-side routing
      fs: {
        // Allow serving files from parent folders, needed for production builds
        allow: ['../'],
      },
    },
    // Ensure proper handling of client-side routing in production
    build: {
      outDir: 'dist',
      assetsDir: 'assets',
      emptyOutDir: true,
      // Sourcemaps ONLY in dev/preview — shipping them to production exposes
      // the entire source tree (variable names, API endpoints, error messages,
      // business logic) to any visitor via DevTools or automated crawlers.
      sourcemap: mode !== 'production',
      rollupOptions: {
        output: {
          manualChunks: undefined,
        },
      },
    },
    define: {
      'import.meta.env.PROD': JSON.stringify(mode === 'production'),
      'import.meta.env.DEV': JSON.stringify(mode !== 'production'),
      // Build identity, so the bundle knows which version of itself it is. Without
      // this the About dialog could only report the *backend* version, which makes a
      // stale cached tab indistinguishable from a current one for users and support.
      // The timestamp is what discriminates two builds of the same release.
      __APP_VERSION__: JSON.stringify(pkg.version),
      __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
    },
    base: '/',
    optimizeDeps: {
      exclude: ['@ffmpeg/ffmpeg', '@ffmpeg/util'],
    },
    worker: {
      format: 'es', // Required for FFmpeg.wasm worker threads
    },
  };
});
