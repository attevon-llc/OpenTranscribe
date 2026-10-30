"""Phase 2 of a re-cluster: embeddings, tenant partitions, cosine matrix and AHC."""

import logging
import math
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple

import numpy as np

from app.core import constants
from app.models.media import MediaFile
from app.models.media import Speaker
from app.services.clustering.recluster_groups import Groups

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)

Report = Callable[[int, int, str, float], None]


# Rows of the similarity chunk processed at a time.
# 500 rows x N cols x 4 bytes keeps each chunk under ~30 MB
# even at 15 000 unlabeled speakers.
SIM_CHUNK = 500


def compute_similarity_groups(
    self: "SpeakerClusteringService",
    emb_rows: list[list[float]],
    ordered_ids: list[int],
    threshold: float,
    report: Any,
) -> dict[int, list[int]]:
    """Cosine similarity matrix + AHC (complete linkage) for one partition.

    PyTorch on CUDA for fast matrix operations, CPU fallback. Called once
    per tenant partition by ``batch_recluster`` — the returned groups map
    AHC labels to speaker ids and include singleton groups (filtered by
    the caller).

    GPU memory budget (worst case 15k x 512-dim float16):
      M           = 15000 x 512 x 2  ~  15 MB VRAM
      M.T         = transposed copy     ~  15 MB VRAM
      sim chunk   = 2000 x 15000 x 2  ~  60 MB VRAM
      peak total  ~  90 MB VRAM (trivial for any GPU)
    """
    # torch and scipy are imported here, not at module scope, per the repo convention
    # for heavy optional deps. This module is reachable from `app.api.router`, so a
    # top-level `import torch` was pulled into every process that imports `app.main` —
    # including all 48 pytest-xdist workers, at ~6.3 s of CPU each (issue #431).
    import torch
    from scipy.cluster.hierarchy import fcluster
    from scipy.cluster.hierarchy import linkage
    from scipy.spatial.distance import squareform

    n = len(emb_rows)
    if n < 2:
        return {}

    # Use GPU for large matrices, CPU for small ones.
    # GPU is faster for matmul but creates a CUDA context (~1.4GB)
    # that persists in prefork worker children. For small speaker
    # counts, CPU is fast enough and avoids the context overhead.
    # The threshold is shared with the dispatch router (issue #1083).
    use_gpu = torch.cuda.is_available() and n >= constants.SPEAKER_CLUSTERING_GPU_MIN_SPEAKERS
    device = torch.device("cuda:0" if use_gpu else "cpu")
    dtype = torch.float16 if use_gpu else torch.float32
    logger.info(
        "Phase 2: computing similarities for %d speakers on %s (dim=%d)",
        n,
        device,
        len(emb_rows[0]),
    )

    report(4, 5, "Computing cosine similarities...", 0.4)

    # Pre-allocate CPU float32 array for full similarity matrix
    emb_matrix = np.array(emb_rows, dtype=np.float32)
    sim_full = np.empty((n, n), dtype=np.float32)

    try:
        M = torch.tensor(
            emb_matrix,
            dtype=dtype,
            device=device,
        )
        del emb_matrix

        # Safe L2-normalize: clamp norms to avoid NaN from zero-length embeddings
        norms = M.norm(dim=1, keepdim=True).clamp(min=1e-8)
        M = M / norms

        # Pre-transpose once — .T is not contiguous, make it so
        MT = M.T.contiguous()

        sim_chunk = 2000 if use_gpu else SIM_CHUNK
        total_chunks = math.ceil(n / sim_chunk)

        for ci in range(total_chunks):
            start = ci * sim_chunk
            end = min(start + sim_chunk, n)

            # (chunk, D) @ (D, N) -> (chunk, N) cosine sims
            sim_block = torch.mm(M[start:end], MT)
            sim_full[start:end] = sim_block.float().cpu().numpy()
            del sim_block

            report(
                4,
                5,
                "Computing cosine similarities...",
                0.4 + 0.3 * (ci + 1) / total_chunks,
            )

        del M, MT
    finally:
        if use_gpu:
            torch.cuda.empty_cache()
            # Release the CUDA context from this prefork child.
            # Without this, each child holds ~1.4GB+ of GPU memory
            # until it exits (max-tasks-per-child).
            torch.cuda.ipc_collect()
            import gc

            gc.collect()
            torch.cuda.empty_cache()

    # --- AHC with complete linkage ---
    report(4, 5, "Clustering with AHC (complete linkage)...", 0.75)

    # Ensure diagonal is exactly 1.0 and matrix is symmetric
    np.fill_diagonal(sim_full, 1.0)
    sim_full = np.maximum(sim_full, sim_full.T)

    # Convert similarity to distance
    dist = 1.0 - sim_full
    np.clip(dist, 0.0, 2.0, out=dist)
    np.fill_diagonal(dist, 0.0)
    del sim_full

    # scipy AHC
    condensed = squareform(dist, checks=False)
    del dist
    Z = linkage(condensed, method="complete")
    del condensed
    labels = fcluster(Z, t=1.0 - threshold, criterion="distance")
    del Z

    # Build groups from fcluster labels
    groups: dict[int, list[int]] = defaultdict(list)
    for idx, label in enumerate(labels):
        groups[int(label)].append(ordered_ids[idx])
    del labels

    return dict(groups)


# ----------------------------------------------------------------------
# Step 3 (Phase 2): GPU-accelerated cosine similarity + AHC
#
# a) Query unlabeled speaker UUIDs from DB
# b) Fetch embeddings from OpenSearch
# c) Partition per tenant scope (the speaker's file org)
# d) Per partition: chunked cosine matmul + AHC (complete
#    linkage) - see compute_similarity_groups
# ----------------------------------------------------------------------


class Unlabeled(NamedTuple):
    """The unlabeled speakers' aligned ids and embeddings, and the embedding cache."""

    ordered_ids: list[int]
    emb_rows: list[list[float]]
    emb_cache: dict[int, list[float]]
    total: int


def load_unlabeled_embeddings(
    self: "SpeakerClusteringService",
    user_id: int,
    off_scopes: set[int | None],
    threshold: float,
) -> Unlabeled:
    from app.services.opensearch_service import iter_speaker_embeddings

    unlabeled_query = self.db.query(Speaker.id, Speaker.uuid).filter(
        Speaker.user_id == user_id,
        Speaker.profile_id.is_(None),
    )
    if off_scopes:
        unlabeled_query = unlabeled_query.join(
            MediaFile, MediaFile.id == Speaker.media_file_id
        ).filter(self._scope_not_in(MediaFile.organization_id, off_scopes))
    unlabeled_rows = unlabeled_query.all()
    unlabeled_uuid_to_id: dict[str, int] = {str(r.uuid): int(r.id) for r in unlabeled_rows}
    unlabeled_uuids = list(unlabeled_uuid_to_id.keys())
    total_unlabeled = len(unlabeled_uuids)

    logger.info(
        "Phase 2: %d unlabeled speakers for user %d (threshold=%.2f)",
        total_unlabeled,
        user_id,
        threshold,
    )

    # Collect embeddings into aligned index arrays
    ordered_ids: list[int] = []
    emb_rows: list[list[float]] = []

    for batch in iter_speaker_embeddings(user_id, speaker_uuids=unlabeled_uuids, batch_size=500):
        for item in batch:
            sid = unlabeled_uuid_to_id.get(item["speaker_uuid"])
            if sid is not None:
                ordered_ids.append(sid)
                emb_rows.append(item["embedding"])

    # Embedding cache: speaker_id -> embedding (reused across phases).
    # Cache embeddings so centroid computation skips OpenSearch
    emb_cache: dict[int, list[float]] = {}
    for i, sid in enumerate(ordered_ids):
        emb_cache[sid] = emb_rows[i]

    # Validate embedding dimensions are consistent
    if emb_rows:
        expected_dim = len(emb_rows[0])
        valid_indices = [i for i, e in enumerate(emb_rows) if len(e) == expected_dim]
        if len(valid_indices) < len(emb_rows):
            skipped = len(emb_rows) - len(valid_indices)
            logger.warning(
                "Skipped %d embeddings with mismatched dimensions (expected %d)",
                skipped,
                expected_dim,
            )
            emb_rows = [emb_rows[i] for i in valid_indices]
            ordered_ids = [ordered_ids[i] for i in valid_indices]
    return Unlabeled(ordered_ids, emb_rows, emb_cache, total_unlabeled)


def ahc_groups_per_tenant(
    self: "SpeakerClusteringService",
    unlabeled: Unlabeled,
    threshold: float,
    report: Report,
) -> tuple[Groups, dict[tuple[int, int], int | None]]:
    """AHC groups of the unlabeled speakers, built PER tenant scope.

    The similarity graph is built per tenant scope (the speaker's FILE org, None =
    personal) so a member's org and personal speakers are never AHC-linked into one
    cluster. Community edition: every file org is NULL -> a single personal partition ->
    identical grouping to the unpartitioned code.
    """
    ordered_ids, emb_rows = unlabeled.ordered_ids, unlabeled.emb_rows
    org_by_speaker: dict[int, int | None] = {}
    if ordered_ids:
        for sid, org in (
            self.db.query(Speaker.id, MediaFile.organization_id)
            .join(MediaFile, MediaFile.id == Speaker.media_file_id)
            .filter(Speaker.id.in_(ordered_ids))
            .all()
        ):
            org_by_speaker[int(sid)] = int(org) if org is not None else None

    partitions: dict[int | None, tuple[list[int], list[list[float]]]] = {}
    for sid, emb_row in zip(ordered_ids, emb_rows, strict=True):
        p_ids, p_rows = partitions.setdefault(org_by_speaker.get(sid), ([], []))
        p_ids.append(sid)
        p_rows.append(emb_row)

    # Groups keyed by (partition index, AHC label); group_org carries
    # each group's tenant scope through to cluster creation.
    groups: Groups = {}
    group_org: dict[tuple[int, int], int | None] = {}
    for p_idx, (scope_org, (p_ids, p_rows)) in enumerate(partitions.items()):
        if len(p_ids) < 2:
            continue
        p_groups = self._compute_similarity_groups(p_rows, p_ids, threshold, report)
        for lbl, g_member_ids in p_groups.items():
            groups[(p_idx, lbl)] = g_member_ids
            group_org[(p_idx, lbl)] = scope_org
        logger.info(
            "Phase 2 AHC (%s): %d groups (>= 2 members: %d) from %d speakers",
            f"org {scope_org}" if scope_org is not None else "personal",
            len(p_groups),
            sum(1 for g in p_groups.values() if len(g) >= 2),
            len(p_ids),
        )
    return groups, group_org
