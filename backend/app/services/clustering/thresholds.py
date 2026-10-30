"""Similarity thresholds shared by the clustering modules."""

# Clustering thresholds
# With complete linkage, 0.75 nearly guarantees same speaker identity
CLUSTER_ASSIGNMENT_THRESHOLD = 0.75
# Cross-gender cluster assignment requires higher similarity to prevent
# gender-mismatched speakers from joining clusters.  A speaker whose
# predicted gender differs from the cluster's dominant gender must exceed
# this threshold instead of CLUSTER_ASSIGNMENT_THRESHOLD.
GENDER_CROSS_THRESHOLD = 0.85
# In batch_recluster post-processing, minority-gender members whose
# embedding similarity to the group centroid falls below this value are
# auto-evicted.  Both signals (gender mismatch + weak embedding) must
# agree — this avoids removing legitimate same-speaker matches.
GENDER_OUTLIER_EVICT_THRESHOLD = 0.70
