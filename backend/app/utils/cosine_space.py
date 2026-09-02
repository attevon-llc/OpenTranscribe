"""Conversions between raw cosine similarity and OpenSearch ``cosinesimil`` scores.

Every kNN field in this app is mapped ``"space_type": "cosinesimil"``, and Lucene's
``cosinesimil`` does **not** report raw cosine — it reports ``(1 + cosine) / 2`` so
that every score is non-negative. Both directions of that conversion are
load-bearing, and both have been got wrong here:

* **Reading** a hit's ``_score`` as a cosine overstates every similarity. The
  repo-wide invariant covers this direction; all read sites convert.
* **Writing** a raw-cosine threshold into a query — ``min_score``, or any other
  filter expressed in score space — *understates* the gate by the same amount.
  ``min_score=0.75`` admits everything at raw cosine ``>= 0.50`` (issue #674), and
  a read-site audit cannot see it, because nothing is being read.

Call these instead of open-coding the arithmetic, so the space a number lives in is
stated by the name at the call site rather than inferred from its surroundings.
"""


def raw_cosine_from_opensearch_score(opensearch_score: float) -> float:
    """Convert an OpenSearch ``cosinesimil`` score to raw cosine similarity.

    Args:
        opensearch_score: A hit's ``_score`` from a ``cosinesimil`` kNN query,
            in ``[0, 1]``.

    Returns:
        The raw cosine similarity, in ``[-1, 1]``.
    """
    return 2.0 * float(opensearch_score) - 1.0


def opensearch_score_from_raw_cosine(raw_cosine: float) -> float:
    """Convert a raw cosine similarity to the OpenSearch ``cosinesimil`` score space.

    Use this for any threshold sent *into* OpenSearch (``min_score`` and friends).
    A raw-cosine value passed through unconverted gates at roughly half the
    similarity it names.

    Args:
        raw_cosine: A cosine similarity, in ``[-1, 1]``.

    Returns:
        The equivalent ``cosinesimil`` score, in ``[0, 1]``.
    """
    return (1.0 + float(raw_cosine)) / 2.0
