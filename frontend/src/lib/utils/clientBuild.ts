import type { BuildIdentity } from '$lib/utils/buildIdentity';

/**
 * The identity of the bundle this tab is running, inlined at build time (vite.config.ts
 * `define`, mirrored in vitest.config.ts). Kept in its own module so a test can substitute a
 * client identity: a `define` constant is replaced in the source and cannot be stubbed.
 */
export const CLIENT_BUILD: BuildIdentity = {
  version: __APP_VERSION__,
  commit: __GIT_SHA__,
};
