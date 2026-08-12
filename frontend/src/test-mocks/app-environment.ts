/**
 * Stub for SvelteKit's `$app/environment` under Vitest.
 *
 * `browser` is `true`: vitest runs every file under jsdom on purpose (see
 * frontend/CLAUDE.md), so a module branching on `!browser` would otherwise take
 * its SSR path — a path this SPA never executes in production. A test that
 * exercises code with no production counterpart passes while proving nothing.
 *
 * Without this alias `$stores/gallery` and `SpeakerPreviewPlayer.svelte` fail
 * to resolve and their test files never run.
 */

export const browser = true;
export const dev = false;
export const building = false;
export const version = '0.0.0-test';
