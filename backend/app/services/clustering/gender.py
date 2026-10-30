"""Gender reads over a cluster: dominant gender, composition summary, outlier analysis."""

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

import numpy as np

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


def analyze_gender_outliers(
    self: "SpeakerClusteringService",
    cluster_uuid: str,
    user_id: int,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> dict:
    """Analyze minority-gender speakers for potential outliers using embedding similarity.

    Uses batch OpenSearch mget + numpy matrix ops for speed.
    """
    from app.services.opensearch_service import get_speaker_embeddings_batch

    cluster = self._scoped_cluster_query(cluster_uuid, user_id, organization_id).first()
    if not cluster:
        raise ValueError("Cluster not found")

    members = (
        self.db.query(SpeakerClusterMember)
        .filter(SpeakerClusterMember.cluster_id == cluster.id)
        .all()
    )

    # Load speakers with gender info
    speaker_ids = [m.speaker_id for m in members]
    speakers = self.db.query(Speaker).filter(Speaker.id.in_(speaker_ids)).all()
    speaker_map = {s.id: s for s in speakers}
    uuid_to_id = {str(s.uuid): s.id for s in speakers}

    # Privacy policy (#1280): the endpoint refuses (409) while voiceprints are off for the
    # cluster's scope. This analysis is also driven by inferred gender, so a member whose
    # inferred gender is withheld takes no part in it.
    self._privacy.preload_files(int(s.media_file_id) for s in speakers)
    member_gender = {s.id: self._privacy.speaker_gender(s) for s in speakers}

    # Determine majority/minority gender
    gender_counts: dict[str, int] = {}
    for s in speakers:
        g = member_gender[s.id]
        if g:
            gender_counts[g] = gender_counts.get(g, 0) + 1

    if len(gender_counts) < 2:
        return {
            "cluster_uuid": cluster_uuid,
            "majority_gender": list(gender_counts.keys())[0] if gender_counts else "unknown",
            "minority_gender": "unknown",
            "minority_analysis": [],
        }

    sorted_genders = sorted(gender_counts.items(), key=lambda x: x[1], reverse=True)
    majority_gender = sorted_genders[0][0]
    minority_gender = sorted_genders[1][0]

    # Batch-fetch all embeddings in one mget call
    speaker_uuids = [str(s.uuid) for s in speakers]
    raw_embeddings = get_speaker_embeddings_batch(speaker_uuids)

    embeddings: dict[int, np.ndarray] = {}
    for suuid, emb in raw_embeddings.items():
        sid = uuid_to_id.get(suuid)
        if sid is not None:
            embeddings[sid] = np.array(emb, dtype=np.float32)

    if not embeddings:
        return {
            "cluster_uuid": cluster_uuid,
            "majority_gender": majority_gender,
            "minority_gender": minority_gender,
            "minority_analysis": [],
        }

    # Vectorized centroid computation
    all_embs = np.array(list(embeddings.values()))
    centroid = all_embs.mean(axis=0)
    centroid /= np.linalg.norm(centroid) + 1e-10

    majority_ids = [
        s.id for s in speakers if member_gender[s.id] == majority_gender and s.id in embeddings
    ]
    minority_ids = [
        s.id for s in speakers if member_gender[s.id] == minority_gender and s.id in embeddings
    ]

    # Build matrices for vectorized similarity
    minority_matrix = np.array([embeddings[sid] for sid in minority_ids])  # (M, D)
    majority_matrix = (
        np.array([embeddings[sid] for sid in majority_ids]) if majority_ids else np.empty((0, 0))
    )

    # Vectorized: all minority sims to centroid at once
    sims_to_centroid = minority_matrix @ centroid  # (M,)

    # Vectorized: all minority avg sims to majority at once
    if majority_matrix.size > 0:
        sim_matrix = minority_matrix @ majority_matrix.T  # (M, N_majority)
        avg_sims_to_majority = sim_matrix.mean(axis=1)  # (M,)
    else:
        avg_sims_to_majority = np.zeros(len(minority_ids))

    # Vectorized: minority peer similarities
    if len(minority_ids) > 1:
        peer_matrix = minority_matrix @ minority_matrix.T  # (M, M)
        # Zero out diagonal (self-similarity) and average remaining
        np.fill_diagonal(peer_matrix, 0.0)
        avg_sims_to_peers = peer_matrix.sum(axis=1) / (len(minority_ids) - 1)
    else:
        avg_sims_to_peers = None

    analysis = []
    for i, sid in enumerate(minority_ids):
        s = speaker_map[sid]
        sim_c = float(sims_to_centroid[i])

        if sim_c < 0.70:
            recommendation = "likely_outlier"
        elif sim_c < 0.80:
            recommendation = "borderline"
        else:
            recommendation = "likely_valid"

        analysis.append(
            {
                "speaker_uuid": str(s.uuid),
                "speaker_name": s.display_name or s.name,
                "predicted_gender": member_gender[s.id],
                "sim_to_centroid": round(sim_c, 4),
                "avg_sim_to_majority": round(float(avg_sims_to_majority[i]), 4),
                "avg_sim_to_minority_peers": round(float(avg_sims_to_peers[i]), 4)
                if avg_sims_to_peers is not None
                else None,
                "outlier_score": round(1.0 - sim_c, 4),
                "recommendation": recommendation,
            }
        )

    return {
        "cluster_uuid": cluster_uuid,
        "majority_gender": majority_gender,
        "minority_gender": minority_gender,
        "minority_analysis": analysis,
    }


def compute_gender_composition(member_items: list[dict]) -> dict:
    """Compute gender composition summary for a list of cluster members."""
    male = sum(1 for m in member_items if m.get("predicted_gender") == "male")
    female = sum(1 for m in member_items if m.get("predicted_gender") == "female")
    total_g = male + female
    unknown = len(member_items) - total_g
    if total_g == 0:
        return {
            "male_count": 0,
            "female_count": 0,
            "unknown_count": unknown,
            "total_with_gender": 0,
            "dominant_gender": None,
            "gender_coherence": None,
            "gender_label": None,
            "has_gender_conflict": False,
        }
    dominant = "male" if male >= female else "female"
    dominant_count = max(male, female)
    coherence = dominant_count / total_g
    conflict = min(male, female) > 0
    if coherence == 1.0:
        label = f"100% {dominant.title()}"
    else:
        label = f"{dominant_count}/{total_g} {dominant.title()}"
    return {
        "male_count": male,
        "female_count": female,
        "unknown_count": unknown,
        "total_with_gender": total_g,
        "dominant_gender": dominant,
        "gender_coherence": round(coherence, 3),
        "gender_label": label,
        "has_gender_conflict": conflict,
    }


def get_cluster_dominant_gender(
    self: "SpeakerClusteringService", cluster: SpeakerCluster
) -> str | None:
    """Get the dominant gender of a cluster's members, as the privacy policy allows it.

    An inferred gender whose owner or organization has gender inference off does not
    vote (a gender a person confirmed always does).
    """
    counts: dict[str, int] = defaultdict(int)
    members = (
        self.db.query(Speaker)
        .join(SpeakerClusterMember, SpeakerClusterMember.speaker_id == Speaker.id)
        .filter(
            SpeakerClusterMember.cluster_id == cluster.id,
            Speaker.predicted_gender.isnot(None),
        )
        .all()
    )
    self._privacy.preload_files(int(m.media_file_id) for m in members)
    for member in members:
        gender = self._privacy.speaker_gender(member)
        if gender:
            counts[gender] += 1
    if not counts:
        return None
    dominant: str | None = max(counts.items(), key=lambda kv: kv[1])[0]
    return dominant
