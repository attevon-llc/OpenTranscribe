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
  // Build edition switch: 'cloud' enables Clerk auth; anything else = community/self-host.
  readonly VITE_DEPLOYMENT_EDITION?: string;
  // Clerk publishable key — required only in the cloud build.
  readonly VITE_CLERK_PUBLISHABLE_KEY?: string;
  // Add other environment variables as needed
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
