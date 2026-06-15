/**
 * Build-edition switch.
 *
 * OpenTranscribe ships from one frontend codebase in two editions:
 *  - **community** (default, self-host): local login, local `UserMFA`, httpOnly
 *    cookie auth. This is the byte-identical legacy behavior.
 *  - **cloud** (managed SaaS): Clerk-hosted auth + MFA, per-request bearer tokens.
 *
 * The edition is fixed at build time via `VITE_DEPLOYMENT_EDITION=cloud`. Anything
 * Clerk-specific MUST be gated by {@link isCloudEdition} (a compile-time constant),
 * so the community bundle never imports/instantiates the Clerk SDK and never needs
 * a publishable key.
 *
 * NOTE: this is the *build* edition. The runtime `capabilities` store
 * (`$stores/capabilities`, fed by `GET /system/capabilities`) reflects what the
 * *backend* reports and is what feature-surface gating should use. The two should
 * agree in a correct deployment; this constant is specifically for deciding which
 * auth machinery (Clerk vs local) the bundle wires up.
 */

/**
 * True only in the cloud build (`VITE_DEPLOYMENT_EDITION=cloud`). A constant, so
 * `if (!isCloudEdition) { ... }` lets Vite tree-shake the Clerk branch out of the
 * community bundle entirely.
 */
export const isCloudEdition: boolean = import.meta.env.VITE_DEPLOYMENT_EDITION === 'cloud';

/** Clerk publishable key (cloud only). Empty/undefined in the community build. */
export const clerkPublishableKey: string = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY ?? '';
