/**
 * Stylelint for global stylesheets (src/**\/*.css) and Svelte `<style>` blocks.
 *
 * Svelte files are parsed with postcss-html, the custom syntax the Stylelint docs name for
 * HTML-like containers (https://stylelint.io/user-guide/get-started, "customSyntax").
 * Formatting is Prettier's: Stylelint 16 removed its stylistic rules, and the layout-only
 * rules stylelint-config-standard still carries (blank lines, comment padding) are off below.
 *
 * Existing findings are ratcheted per file by scripts/guards/check_stylelint_ratchet.py
 * (baseline scripts/guards/baselines/stylelint.txt, which can only shrink). Every rule here is
 * an error: the ratchet counts errors per `path::rule`, and a file/rule pair not in the
 * baseline fails.
 *
 * @type {import('stylelint').Config}
 */
export default {
  extends: ['stylelint-config-standard'],
  ignoreFiles: [
    'build/**',
    'dist/**',
    '.svelte-kit/**',
    'node_modules/**',
    'static/**',
    'ffmpeg-wasm-build/**',
  ],
  overrides: [{ files: ['**/*.svelte'], customSyntax: 'postcss-html' }],
  rules: {
    // Correctness rules the lane exists for. All are in stylelint-config-recommended already;
    // they are restated so a change to the shared config cannot silently drop one.
    'no-descending-specificity': true,
    'declaration-block-no-duplicate-properties': [
      true,
      { ignore: ['consecutive-duplicates-with-different-syntaxes'] },
    ],
    'no-duplicate-selectors': true,
    'property-no-unknown': true,
    'unit-no-unknown': true,
    'color-no-invalid-hex': true,
    // `:global(...)` is Svelte's scoping escape hatch, not a CSS pseudo-class.
    'selector-pseudo-class-no-unknown': [true, { ignorePseudoClasses: ['global'] }],
    'at-rule-no-unknown': true,

    // Layout-only rules: whitespace and blank lines are Prettier's job.
    'at-rule-empty-line-before': null,
    'comment-empty-line-before': null,
    'comment-whitespace-inside': null,
    'custom-property-empty-line-before': null,
    'declaration-empty-line-before': null,
    'rule-empty-line-before': null,

    // The `--<family>-color-rgb` tokens are comma-separated channel lists
    // (`--primary-color-rgb: 37, 99, 235`) consumed as `rgba(var(--x-rgb), α)`, the tint rule in
    // src/styles/CLAUDE.md. Modern space/slash notation cannot take a comma list, so the legacy
    // function and its numeric alpha are the repo convention, not drift.
    'color-function-notation': null,
    'color-function-alias-notation': null,
    'alpha-value-notation': null,

    // Media queries are written `(max-width: 768px)` throughout (170 of them). Range syntax
    // (`(width <= 768px)`) needs Safari 16.4; only the production minify step could lower it,
    // and `npm run dev` serves the source CSS as written.
    'media-feature-range-notation': 'prefix',
    // `:not(.a):not(.b)` -> `:not(.a, .b)` is not a notation change: the list form takes the
    // specificity of its most specific argument instead of the sum, which reorders the cascade.
    'selector-not-notation': null,
    // These assume a prefixer runs on every build. Here only `vite build` does (Lightning CSS
    // minify, targets from build.cssTarget, https://vite.dev/config/build-options#build-cssminify);
    // `npm run dev` serves source CSS unchanged, so dropping the hand-written prefixes
    // (`-webkit-user-select`, `-webkit-backdrop-filter`, `-webkit-box-decoration-break`) would make
    // Safari render dev and prod differently.
    'property-no-vendor-prefix': null,
    'value-no-vendor-prefix': null,
    'selector-no-vendor-prefix': null,
    'at-rule-no-vendor-prefix': null,
    'media-feature-name-no-vendor-prefix': null,
    // Hex length is cosmetic, and the theme tests compare token values as written across
    // theme.css and app.html (boot-vars.test.ts), so `#ffffff` stays `#ffffff`.
    'color-hex-length': null,
    // Font-family names in the `--font-*` stacks are names, not keywords.
    'value-keyword-case': ['lower', { ignoreProperties: ['/^--font/'] }],
    // BEM modifiers and elements (`.trace-marker--dot`, `.range-slider__label`) are kebab-case
    // blocks with a `__element` / `--modifier` suffix, a convention this codebase uses on purpose.
    'selector-class-pattern': [
      '^[a-z][a-z0-9]*(-[a-z0-9]+)*(__[a-z0-9]+(-[a-z0-9]+)*)?(--[a-z0-9]+(-[a-z0-9]+)*)?$',
      {
        message: (selector) =>
          `Expected class selector "${selector}" to be kebab-case (BEM __/-- allowed)`,
      },
    ],
  },
  reportNeedlessDisables: true,
  reportInvalidScopeDisables: true,
  reportDescriptionlessDisables: true,
};
