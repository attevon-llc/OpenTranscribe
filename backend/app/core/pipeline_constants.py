"""Tunables shared by several audio/ASR/task modules, each defined exactly once.

Every constant here used to be an independent per-module copy. Dependency-free on
purpose so the lightest modules (``transcription/crisperwhisper_normalize``) can import it.
"""

#: Sample rate (Hz) of the mono audio every decode path hands to the models.
SAMPLE_RATE = 16000

#: Maximum plausible duration of a single word (seconds); longer word timestamps are capped.
MAX_WORD_DURATION = 5.0

#: Cloud ASR word grouping: a silence gap this long (seconds) starts a new segment.
ASR_SEGMENT_MIN_GAP_SECONDS = 0.5

#: Cloud ASR word grouping: a segment reaching this duration (seconds) is force-split.
ASR_SEGMENT_MAX_DURATION_SECONDS = 30.0

#: Files grouped into one Celery batch task by the speaker migration / repair dispatchers.
FILES_PER_BATCH_TASK = 25
