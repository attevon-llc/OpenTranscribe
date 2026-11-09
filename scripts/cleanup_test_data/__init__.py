"""Signature-scoped cleanup of orphaned test data (issue #629).

``scripts/cleanup-test-users.py`` only ever swept user rows (and, later, leaked LLM
configs) — every OTHER kind of test-minted data (uploaded media, collections, tags,
watch sources, speaker profiles, chat conversations) had no sweep at all and
accumulated silently on the shared dev stack. This script is the sibling that covers
the rest, and is the ONE entry point a caller should invoke — it shells out to
``cleanup-test-users.py`` for the user/LLM-config plane (same idiom
``run-integration-tests.sh`` already uses) rather than duplicating that logic.

Why a media sweep cannot be pure SQL: child rows (``transcript_segment``,
``analytics``, ``comment``, ``task``) reference ``media_file.id`` with
``ON DELETE NO ACTION``, and MinIO objects + OpenSearch documents (transcript index,
``transcript_chunks``, speaker embeddings) are unreachable from Postgres at all. The
single canonical destroyer is ``purge_media_file()``
(``backend/app/services/file_cleanup_service.py``), reached only via
``DELETE /api/files/{uuid}`` (falling back to ``/force``). So candidates are read
from Postgres (authoritative, sees every owner, no pagination) and deletions are
issued over the HTTP API — for media AND for every other Tier A resource type below,
so cascade/embedding cleanup on each resource's own delete path always runs.

Safety tiers (see the issue #629 plan for the full reasoning):

* **Tier A (unambiguous)** — filename/name/title shapes with a random hex suffix, or
  an RFC 2606 ``.invalid`` email TLD. No human types one of these by hand. Selected by
  ``--execute-unambiguous``.
* **Tier B (review)** — plausible hand-created names (``testuser_%``,
  ``test-%@example.com``). Deletion needs the full ``--execute``.
* **Tier C** — report-count only, never deleted here: OpenSearch orphans (should not
  accumulate once every media delete routes through the API) and ``refresh_token``
  growth (a separate bug — see the issue this script's docstring references).

A run's own data is distinguished from a concurrently LIVE run's data by TIME, not by
name — see ``scripts/testrun-registry.sh``. Every candidate table already carries a
creation timestamp, so the sweep cutoff is ``min(started_at)`` across every currently
live run marker (including this run's own), and only rows older than that are ever
touched.

Store planes (issue #1195, ``scripts/lib/test_artifact_sweep.py``): besides ROWS, a killed
run leaves artifacts no API reaches — throwaway OpenSearch indexes and ingest pipelines,
MinIO buckets and test-keyed objects, scratch Postgres databases, ``/tmp`` trees, and labelled
throwaway docker containers/networks. Same tiers, same liveness cutoff, plus: a name must FULLY
match a registered test shape, protected names (production indexes, configured buckets, the
target database, ``.``-prefixed system indexes) are refused before any shape is consulted, and
every delete re-classifies its target first. Docker is Tier B only (never automatic). The store
planes refuse to run (exit 2) when the exported ``*_PORT`` values point at different stacks.

Usage (from repo root, inside backend venv):
    python scripts/cleanup-test-data.py                        # dry run, all planes, all tiers
    python scripts/cleanup-test-data.py --execute-unambiguous   # delete Tier A only
    python scripts/cleanup-test-data.py --execute               # delete Tier A + Tier B
    python scripts/cleanup-test-data.py --planes indexes,tmp    # a subset of planes
    python scripts/cleanup-test-data.py --json                  # machine-readable report

Against an isolated stack, export every port it uses (``POSTGRES_PORT``, ``OPENSEARCH_PORT``,
``MINIO_PORT``, ``E2E_BACKEND_URL``), as ``./opentr.sh --fresh --port-offset N`` sets them.

``scripts/cleanup-test-data.py`` is the entry point; this package holds the implementation.
"""
