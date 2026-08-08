/**
 * The tag manager is client-only, like every data-loading route in this SPA:
 * it reads `/tags` with the caller's session, which does not exist at build
 * time. There is no server to run a `load` on in production.
 */
export const ssr = false;
