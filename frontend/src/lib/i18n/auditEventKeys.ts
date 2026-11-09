// Kept out of keyMaps.ts: only the organization audit views read it, and keyMaps is in the root
// layout's static closure (bundle budget, scripts/check-bundle-budget.mjs).

/**
 * The audit events an organization admin sees, by wire identifier (issue #1277). An event this
 * build has no string for renders as its raw identifier (`labelFor`), never a dotted key.
 */
export const ORG_AUDIT_EVENT_KEYS: Record<string, string> = {
  'content.file.view': 'orgAudit.event.contentFileView',
  'content.media.access': 'orgAudit.event.contentMediaAccess',
  'content.export': 'orgAudit.event.contentExport',
  'collection.membership.change': 'orgAudit.event.membershipChange',
  'collection.visibility.change': 'orgAudit.event.visibilityChange',
  'resource.share': 'orgAudit.event.resourceShare',
  'resource.unshare': 'orgAudit.event.resourceUnshare',
  'admin.file.quarantine': 'orgAudit.event.fileQuarantine',
  'admin.file.release': 'orgAudit.event.fileRelease',
  'audit.export': 'orgAudit.event.auditExport',
  'content.hold.place': 'orgAudit.event.holdPlace',
  'content.hold.release': 'orgAudit.event.holdRelease',
  'content.hold.refused': 'orgAudit.event.holdRefused',
  'content.hold.annotation': 'orgAudit.event.holdAnnotation',
  'content.transcript.edit': 'orgAudit.event.transcriptEditedHeld',
  'transcript.view_unredacted': 'orgAudit.event.transcriptRevealed',
  'org.policy.change': 'orgAudit.event.policyChange',
  'privacy.voiceprints.purge': 'orgAudit.event.voiceprintsPurged',
  'privacy.gender.purge': 'orgAudit.event.genderPurged',
  'privacy.labels.resync': 'orgAudit.event.labelsResynced',
  'privacy.identity_source.reverify': 'orgAudit.event.identitySourceReverified',
  'gdpr.erasure.scheduled': 'orgAudit.event.erasureScheduled',
  'gdpr.erasure.cancelled': 'orgAudit.event.erasureCancelled',
  'gdpr.erasure.execution_started': 'orgAudit.event.erasureStarted',
  'org.owner.change': 'orgAudit.event.ownerChange',
  'org.content.transfer': 'orgAudit.event.contentTransfer',
};
