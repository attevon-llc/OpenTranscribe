import { describe, it, expect } from 'vitest';
import type { CapabilitiesState } from '$stores/capabilities';
import { buildSectionAccess } from './sectionCapabilities';

const state = (disabled: string[] = []): CapabilitiesState => ({
  edition: 'community',
  loaded: true,
  capabilities: Object.fromEntries(disabled.map((k) => [k, false])),
  audience: {},
  maxUploadBytes: undefined,
  apiMediatedUploadEnabled: true,
});

describe('buildSectionAccess', () => {
  it('is fail-open: an unknown capability reads as enabled (community edition)', () => {
    const access = buildSectionAccess(state(), true, false);
    expect(access.transcription).toEqual({
      prefsCap: true,
      asrCap: true,
      modelSelectionCap: true,
      vocabCap: true,
    });
    expect(access.data).toMatchObject({ isAdmin: true, isSuperAdmin: false, backupCap: true });
  });

  it('turns exactly the disabled capability off, in the section that reads it', () => {
    const access = buildSectionAccess(state(['admin.backup', 'redaction.policy']), true, true);
    expect(access.data.backupCap).toBe(false);
    expect(access.data.retentionCap).toBe(true);
    expect(access.privacy.policyCap).toBe(false);
    expect(access.privacy.userCap).toBe(true);
    expect(access.status.taskHealthCap).toBe(true);
  });
});
