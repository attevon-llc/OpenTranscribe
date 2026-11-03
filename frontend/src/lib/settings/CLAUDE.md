# src/lib/settings — view rules for the Settings modal and its panels

## Purpose

The rules behind `components/settings/*`, kept out of the components so they are testable without
rendering. The server decides every outcome; these modules pick copy, tabs and what is worth showing.

## Key files (by panel)

- Modal plumbing: `settingsModalLoader.ts` (in the root layout's static closure; bundle budget),
  `sectionLoaders.ts` (lazy section chunks), `sectionData.ts` (stale-while-revalidate GET cache),
  `sectionAliases.ts` (ids with no sidebar row), `returnToSettings.ts`, `idleQueue.ts`, and the
  per-section tab lists `*Tabs.ts` (transcription, speaker identification, privacy & redaction,
  data management, file status, watch sources).
- Processing pipeline (#1227): `pipelineSteps.ts`, `pipelineIssues.ts`, `pipelineKeys.ts` (explicit
  i18n literals, proven by `keyMaps.test.ts`), `speakerSources.ts`, `asrCapabilities.ts`,
  `asrOutcome.ts`, `perFileModel.ts`, `pipelineViewFixtures.ts` (test builders).
- Local models: `localModelView.ts`, `localModelActivation.ts` (#1246).
- LLM panel: `llmSettingsView.ts`, `llmProbes.ts` (connection / reasoning / context probes),
  `llmModelDiscovery.ts`, `llmConfigForm.ts`.
- Prompt library: `promptList.ts`, `promptForm.ts`, `shareState.ts` (optimistic share toggle, shared
  with the LLM config list).
- Credentials and privacy: `hfToken.ts`, `pyannoteCredential.ts`, `privacyPolicy.ts`.

## Conventions / patterns

- Pure and testable; a colocated `*.test.ts` locks each module.
