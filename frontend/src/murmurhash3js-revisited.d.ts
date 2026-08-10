// `murmurhash3js-revisited` ships no types. Only the x64 128-bit variant is used
// (by `$lib/services/fileFingerprint`), so declare just that surface rather than
// pulling in a @types package for two lines of API.
declare module 'murmurhash3js-revisited' {
  const murmur: {
    x64: {
      /** 32-char lowercase hex digest of the 128-bit x64 hash. */
      hash128(bytes: Uint8Array, seed?: number): string;
    };
  };
  export default murmur;
}
