---
description: Fix frontend build, type-check, lint, and test failures in the OpenTranscribe SvelteKit SPA. Use when svelte-check, eslint, vitest, or the Vite build fails, or when the frontend-check pre-commit hook blocks a commit.
---

# Fix Frontend Build Errors

You are fixing frontend build and type-check errors in the OpenTranscribe project.

## Project Context

- **Framework**: SvelteKit 2 + Svelte with TypeScript
- **Frontend directory**: `frontend/`
- **Build tool**: Vite (config at `frontend/vite.config.ts`)
- **Type checker**: svelte-check (runs via `npm run check` in `frontend/`)
- **Build command**: `npm run build` in `frontend/`
- **TypeScript config**: `frontend/tsconfig.json` (strict: false, checkJs: false)
- **Svelte config**: `frontend/svelte.config.js` (static adapter, SPA mode)
- **Path aliases**: `$lib` -> `./src/lib`, `$components` -> `./src/components`, `$stores` -> `./src/stores`

## Step 1: Diagnose

Run the type checker to identify current errors:

```bash
cd frontend && npx svelte-check --tsconfig ./tsconfig.json --threshold warning
```

If errors are found, also run the build to catch additional issues:

```bash
cd frontend && npm run build
```

## Step 2: Analyze Errors

Common error patterns in this codebase:

### TypeScript errors in Svelte components
- **Missing properties in Record types**: Check if a `Record<SomeType, ...>` has all required keys. Look at the union type definition to find missing keys.
- **`onMount` return type mismatch**: If `onMount(async () => {...})` returns a cleanup function, async functions return `Promise<() => void>` which Svelte ignores. Make `onMount` synchronous, run async logic in an IIFE, and return cleanup synchronously.
- **Missing type imports**: Check that types used in props are properly imported.
- **Implicit any**: Even with `strict: false`, some contexts still flag implicit any.

### Vite build errors
- **Import resolution failures**: Check that imported modules exist and path aliases (`$lib`, `$components`, `$stores`) are used correctly.
- **SSR compatibility**: This is an SPA with `adapter-static`. Use `browser` from `$app/environment` or `typeof window !== 'undefined'` for browser-only APIs.
- **Asset reference errors**: Static assets must be in `frontend/static/` or imported properly.

### Svelte-specific errors
- **Component prop types**: Check that parent components pass the correct prop types.
- **Store subscriptions**: Use `$storeName` syntax in `.svelte` files, `get(storeName)` in `.ts` files.

## Step 3: Fix

For each error:
1. Read the file mentioned in the error output
2. Understand the surrounding context (at least 20 lines around the error)
3. Apply the minimal fix that resolves the error without changing functionality
4. Verify the fix does not introduce new issues by checking related imports and usages

## Step 4: Verify

After making all fixes, re-run the checks:

```bash
cd frontend && npx svelte-check --tsconfig ./tsconfig.json --threshold warning
```

And if the type check passes:

```bash
cd frontend && npm run build
```

Report the results. If errors remain, iterate on fixes.

## Important Guidelines

- **Minimal changes**: Only fix what is broken. Do not refactor or "improve" surrounding code.
- **Preserve functionality**: Every fix must maintain existing behavior.
- **Light/dark mode**: If touching CSS or styles, ensure both themes work.
- **i18n**: Do not hardcode English strings. Use the i18n system if adding user-visible text.
- **No config changes**: Do not modify `tsconfig.json` or `svelte.config.js` to suppress errors.
