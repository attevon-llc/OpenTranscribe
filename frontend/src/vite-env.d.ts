/// <reference types="@sveltejs/kit" />

// See https://kit.svelte.dev/docs/types#app
// for information about these interfaces
declare namespace App {
  // interface Error {}
  // interface Locals {}
  // interface PageData {}
  // interface Platform {}
}

// Vite environment variables
declare namespace NodeJS {
  interface ProcessEnv {
    readonly NODE_ENV: 'development' | 'production' | 'test';
  }
}

interface ImportMetaEnv {
  readonly PROD: boolean;
  readonly DEV: boolean;
  readonly MODE: string;
  // Optional, env-gated error monitoring (see src/lib/monitoring.ts). Unset by default.
  readonly VITE_SENTRY_DSN?: string;
  readonly VITE_SENTRY_ENVIRONMENT?: string;
  readonly VITE_SENTRY_TRACES_SAMPLE_RATE?: string;
  // Build edition switch: 'cloud' enables the managed edition's hosted auth +
  // billing UI (via the $lib/cloud seam); anything else = community/self-host.
  // The managed overlay augments ImportMetaEnv with its own vars (env.d.ts).
  readonly VITE_DEPLOYMENT_EDITION?: string;
  // Add other environment variables as needed
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Build identity, injected by `define` in vite.config.ts. These are compile-time
// constants — they are inlined into the bundle, so a running tab always reports the
// build it was compiled from (see AboutModal's frontend/backend version comparison).
/** Frontend package version at build time (e.g. `0.4.1`). */
declare const __APP_VERSION__: string;
/** ISO-8601 timestamp of the build that produced this bundle. */
declare const __BUILD_TIME__: string;
