/**
 * Build-edition switch.
 *
 * OpenTranscribe ships from one frontend codebase in two editions:
 *  - **community** (default, self-host): local login, local `UserMFA`, httpOnly
 *    cookie auth. This is the byte-identical legacy behavior.
 *  - **cloud** (managed SaaS): hosted external auth + MFA, per-request bearer
 *    tokens, billing/usage UI — all provided by the `$lib/cloud` seam module,
 *    whose community version is a no-op stub replaced at image-build time by
 *    the commercial overlay.
 *
 * The edition is fixed at build time via `VITE_DEPLOYMENT_EDITION=cloud`.
 * Anything managed-edition-specific MUST be gated by {@link isCloudEdition}
 * (a compile-time constant), so the community bundle never imports the hosted
 * auth SDK and never needs its publishable key.
 *
 * NOTE: this is the *build* edition. The runtime `capabilities` store
 * (`$stores/capabilities`, fed by `GET /system/capabilities`) reflects what the
 * *backend* reports and is what feature-surface gating should use. The two should
 * agree in a correct deployment; this constant is specifically for deciding which
 * auth machinery (hosted external vs local) the bundle wires up.
 */

/**
 * True only in the cloud build (`VITE_DEPLOYMENT_EDITION=cloud`). A constant, so
 * `if (!isCloudEdition) { ... }` lets Vite tree-shake the managed-edition branch
 * out of the community bundle entirely.
 */
export const isCloudEdition: boolean = import.meta.env.VITE_DEPLOYMENT_EDITION === 'cloud';
