import axios from 'axios';
import { describe, expect, it } from 'vitest';
import { takeNetworkAttempts } from './test-network-guard';

describe('network guard', () => {
  it('rejects fetch and names method and URL', async () => {
    await expect(fetch('http://example.invalid/a', { method: 'post' })).rejects.toThrow(
      /POST http:\/\/example\.invalid\/a/
    );
    expect(takeNetworkAttempts()).toEqual(['POST http://example.invalid/a']);
  });

  it('rejects an axios (XHR) request and names method and URL', async () => {
    await expect(axios.get('http://example.invalid/b')).rejects.toThrow(
      /GET http:\/\/example\.invalid\/b/
    );
    expect(takeNetworkAttempts()).toEqual(['GET http://example.invalid/b']);
  });
});
