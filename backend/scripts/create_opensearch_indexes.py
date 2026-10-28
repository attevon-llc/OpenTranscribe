"""Create the OpenSearch indexes by hand, using the application's own bootstrap.

Backend startup already does this, so the script is only for provisioning before the API has
ever started. It calls the same functions startup calls, so the index names (``transcripts``,
the versioned ``speakers_v*`` pair with its ``speakers`` alias, ``transcript_chunks``) and their
mappings can never drift from what the application reads and writes. The earlier standalone
version hand-built a second, divergent ``transcripts``/``speakers`` pair with float-array
embeddings and no kNN mapping, which a current deployment cannot use.

Connection settings come from the app's one shared builder (``app.core.opensearch_auth``), which
the package's client singleton uses on import. Run inside the backend container:
``./opentr.sh shell backend`` then ``python scripts/create_opensearch_indexes.py``.
"""

from app.services.opensearch_service import ensure_indices_exist
from app.services.opensearch_service import ensure_v4_index_exists
from app.services.search.indexing_service import ensure_chunks_index_exists


def main() -> int:
    # ensure_indices_exist() logs and swallows its own failures (it must never take startup
    # down), so the chunk index's boolean result is the only status this script can report.
    ensure_indices_exist()
    ensure_v4_index_exists()
    if not ensure_chunks_index_exists():
        print("Could not create the transcript chunks index; see the log output above")
        return 1
    print("OpenSearch indexes are in place")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
