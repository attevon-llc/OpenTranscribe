// Only madge reads this file (see .madgerc). Madge drops tsconfig `paths` when it loads a tsconfig,
// so SvelteKit's alias entries (kit.config.js) are mirrored here to let the cycle guard see every import edge.
module.exports = {
  resolve: {
    extensions: ['.ts', '.js', '.svelte', '.json'],
    alias: {
      $lib: `${__dirname}/src/lib`,
      $components: `${__dirname}/src/components`,
      $stores: `${__dirname}/src/stores`,
      $styles: `${__dirname}/src/styles`,
      $assets: `${__dirname}/src/assets`,
      $testMocks: `${__dirname}/src/test-mocks`,
    },
  },
};
