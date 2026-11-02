// Only madge reads this file (see .madgerc). Madge drops tsconfig `paths` when it loads a tsconfig,
// so SvelteKit's kit.alias entries are mirrored here to let the cycle guard see every import edge.
module.exports = {
  resolve: {
    extensions: ['.ts', '.js', '.svelte', '.json'],
    alias: {
      $lib: `${__dirname}/src/lib`,
      $components: `${__dirname}/src/components`,
      $stores: `${__dirname}/src/stores`,
    },
  },
};
