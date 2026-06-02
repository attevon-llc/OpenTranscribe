/**
 * Optional, env-gated error reporting (Sentry).
 *
 * ZERO-impact by default: when `VITE_SENTRY_DSN` is unset (the home-label / self-hosted
 * default), {@link initMonitoring} is a no-op and nothing Sentry-related is referenced, so
 * the default bundle stays clean — there is no eager Sentry import anywhere.
 *
 * The Sentry SDK is intentionally NOT a dependency of this repo. This app is a static,
 * SSR-less SPA (`@sveltejs/adapter-static`), and pulling in a meeting-capture-sized error
 * SDK to bloat a privacy-first self-hosted bundle isn't worth it for an opt-in feature.
 * Operators who want reporting can install the SDK and wire it in at the marked point — the
 * gate and call site already exist, so it's a one-line enablement, not a refactor.
 */

let initialized = false;

/**
 * Initialize optional error monitoring if (and only if) `VITE_SENTRY_DSN` is configured.
 *
 * Safe to call once on app startup. Idempotent and best-effort: any failure is logged and
 * swallowed so monitoring can never break the app it's meant to observe.
 */
export async function initMonitoring(): Promise<void> {
  const dsn = import.meta.env.VITE_SENTRY_DSN;
  if (!dsn || initialized) return;
  initialized = true;

  try {
    // To enable: `npm i @sentry/svelte`, then uncomment the lazy import below. The dynamic
    // import keeps Sentry out of the main chunk — it is only fetched when a DSN is set.
    //
    //   const Sentry = await import('@sentry/svelte');
    //   Sentry.init({
    //     dsn,
    //     environment: import.meta.env.VITE_SENTRY_ENVIRONMENT || import.meta.env.MODE,
    //     tracesSampleRate: Number(import.meta.env.VITE_SENTRY_TRACES_SAMPLE_RATE ?? 0),
    //   });
    //
    // Until the dependency is installed, surface the configured intent without crashing.
    console.info('[monitoring] VITE_SENTRY_DSN set; install @sentry/svelte to enable reporting.');
  } catch (error) {
    console.warn('[monitoring] Failed to initialize error reporting:', error);
  }
}
