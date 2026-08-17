import json
import logging
import os

from app.core.celery import celery_app
from app.core.constants import UtilityPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.utils.transcript_comparison import compare_transcripts
from app.utils.transcript_comparison import export_baseline
from app.utils.uuid_helpers import get_by_uuid_optional

logger = logging.getLogger(__name__)

BENCHMARKS_DIR = os.environ.get("BENCHMARKS_DIR", "/tmp/benchmarks")  # noqa: S108  # nosec B108


@celery_app.task(name="export_transcript_baseline", priority=UtilityPriority.DEV_TOOLS)
def export_baseline_task(file_uuid: str, label: str = "baseline"):
    """Export current transcript data as a baseline snapshot."""
    os.makedirs(BENCHMARKS_DIR, exist_ok=True)

    with session_scope() as db:
        # get_by_uuid_optional, NOT get_file_by_uuid: the latter raises
        # fastapi.HTTPException on a missing/malformed uuid instead of returning a
        # falsy value, which made the `if not media_file` guard below dead code and
        # let an HTTPException escape this Celery task instead of the intended
        # graceful {"status": "error", ...} return.
        media_file = get_by_uuid_optional(db, MediaFile, file_uuid)
        if not media_file:
            logger.error(f"Media file {file_uuid} not found")
            return {"status": "error", "message": f"File {file_uuid} not found"}

        file_id = int(media_file.id)
        short_uuid = file_uuid[:8]
        output_path = os.path.join(BENCHMARKS_DIR, f"{short_uuid}_{label}.json")

        result = export_baseline(db, file_id, output_path)
        logger.info(
            f"Exported baseline for {file_uuid}: {result['segment_count']} segments, "
            f"{result['speaker_count']} speakers -> {output_path}"
        )
        return {"status": "success", "path": output_path, "segment_count": result["segment_count"]}


@celery_app.task(name="compare_transcript_baseline", priority=UtilityPriority.DEV_TOOLS)
def compare_baseline_task(file_uuid: str, baseline_label: str, current_label: str):
    """Compare two transcript snapshots and log results."""
    short_uuid = file_uuid[:8]
    baseline_path = os.path.join(BENCHMARKS_DIR, f"{short_uuid}_{baseline_label}.json")
    current_path = os.path.join(BENCHMARKS_DIR, f"{short_uuid}_{current_label}.json")

    for path, name in [(baseline_path, "Baseline"), (current_path, "Current")]:
        if not os.path.exists(path):
            logger.error(f"{name} file not found: {path}")
            return {"status": "error", "message": f"{name} not found at {path}"}

    try:
        with open(baseline_path) as f:
            baseline = json.load(f)
        with open(current_path) as f:
            current = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Snapshot file is corrupted: {e}")
        return {"status": "error", "message": f"Corrupted snapshot file: {e}"}

    comparison = compare_transcripts(baseline, current)

    # Save comparison report
    comparisons_dir = os.path.join(BENCHMARKS_DIR, "comparisons")
    os.makedirs(comparisons_dir, exist_ok=True)
    report_path = os.path.join(comparisons_dir, f"{current_label}_vs_{baseline_label}.json")
    with open(report_path, "w") as f:
        json.dump(comparison, f, indent=2)

    # Log summary
    logger.info(
        f"Comparison {current_label} vs {baseline_label}: "
        f"text_overlap={comparison['text_word_overlap_avg']:.1f}%, "
        f"speaker_consistency={comparison['speaker_consistency_pct']:.1f}%, "
        f"timestamp_MAE={comparison['timestamp_start_mae_seconds']:.2f}s, "
        f"pass={comparison['pass_overall']}"
    )

    return {"status": "success", "comparison": comparison, "report_path": report_path}
