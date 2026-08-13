"""Analytics computation service for media files.

This service computes speaker analytics, talk time statistics, and other metrics
that were previously computed on the frontend.

The service provides comprehensive analytics computation including:
- Speaker talk time analysis and percentage breakdowns
- Interruption detection and frequency tracking
- Turn-taking patterns and conversation flow analysis
- Question asking frequency by speaker
- Speaking pace calculations (words per minute)
- Silence ratio analysis for meeting efficiency
- Word count statistics across speakers

All analytics are computed server-side to ensure consistency and reduce
frontend complexity while providing detailed insights for meeting analysis.

Example:
    Basic usage for computing analytics for a media file:

    analytics = AnalyticsService.compute_analytics(db, media_file_id)
    if analytics:
        AnalyticsService.save_analytics(db, media_file_id, analytics)

Classes:
    AnalyticsService: Main service class for analytics computation.
"""

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.media import Analytics
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.schemas.media import InterruptionStats
from app.schemas.media import OverallAnalytics
from app.schemas.media import QuestionStats
from app.schemas.media import SpeakerTimeStats
from app.schemas.media import TurnTakingStats

logger = logging.getLogger(__name__)


class AnalyticsService:
    """Service for computing and managing analytics for media files."""

    @staticmethod
    def compute_analytics(db: Session, media_file_id: int) -> OverallAnalytics | None:
        """
        Compute comprehensive analytics for a media file.

        Args:
            db: Database session
            media_file_id: ID of the media file to analyze

        Returns:
            OverallAnalytics object or None if computation fails
        """
        try:
            # Get the media file
            media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
            if not media_file:
                logger.error(f"Media file {media_file_id} not found")
                return None

            # Get transcript segments
            segments = (
                db.query(TranscriptSegment)
                .filter(TranscriptSegment.media_file_id == media_file_id)
                .order_by(
                    TranscriptSegment.start_time,
                    TranscriptSegment.end_time,
                    TranscriptSegment.id,
                )
                .all()
            )

            if not segments:
                logger.warning(f"No transcript segments found for media file {media_file_id}")
                return OverallAnalytics()

            # Compute analytics
            analytics = AnalyticsService._compute_from_segments(
                segments, float(media_file.duration or 0)
            )

            logger.info(
                f"Computed analytics for media file {media_file_id}: "
                f"{analytics.word_count} words, {len(analytics.talk_time.by_speaker)} speakers"
            )
            return analytics

        except Exception as e:
            logger.error(f"Error computing analytics for media file {media_file_id}: {e}")
            return None

    @staticmethod
    def _compute_from_segments(
        segments: list[TranscriptSegment], total_duration: float
    ) -> OverallAnalytics:
        """Compute comprehensive analytics from transcript segments.

        This method processes all transcript segments to calculate detailed analytics
        including speaker participation, interruption patterns, speaking pace,
        and conversation dynamics.

        Args:
            segments: List of TranscriptSegment objects ordered by start_time.
            total_duration: Total duration of the media file in seconds.

        Returns:
            OverallAnalytics object containing all computed statistics including:
            - Speaker talk time breakdowns
            - Interruption counts and patterns
            - Turn-taking statistics
            - Question frequency analysis
            - Overall speaking pace (words per minute)
            - Silence ratio calculation

        Note:
            - Interruptions are detected when speakers overlap in time
            - Questions are identified by text ending with '?'
            - Speaking pace is calculated as total words / total talk time
            - Silence ratio is (total_duration - talk_time) / total_duration
        """
        # Initialize counters
        speaker_times: dict[str, float] = {}
        speaker_words: dict[str, int] = {}
        speaker_turns: dict[str, int] = {}
        speaker_interruptions: dict[str, int] = {}
        speaker_questions: dict[str, int] = {}

        total_words = 0
        total_talk_time = 0.0
        total_interruptions = 0
        total_questions = 0

        previous_speaker = None

        for i, segment in enumerate(segments):
            # Get speaker key for analytics (always use SPEAKER_## format for consistency)
            segment_speaker = AnalyticsService._get_analytics_speaker_key(segment)

            # Calculate segment duration and word count
            segment_duration = float((segment.end_time or 0) - (segment.start_time or 0))
            words = segment.text.split() if segment.text else []
            word_count = len([word for word in words if word.strip()])

            # Update speaker stats
            speaker_times[segment_speaker] = (
                speaker_times.get(segment_speaker, 0) + segment_duration
            )
            speaker_words[segment_speaker] = speaker_words.get(segment_speaker, 0) + word_count
            speaker_turns[segment_speaker] = speaker_turns.get(segment_speaker, 0) + 1

            # Check for questions (simple heuristic)
            if segment.text and segment.text.strip().endswith("?"):
                speaker_questions[segment_speaker] = speaker_questions.get(segment_speaker, 0) + 1
                total_questions += 1

            # Check for interruptions (speaker changes with overlapping times)
            if previous_speaker and previous_speaker != segment_speaker and i > 0:
                previous_segment = segments[i - 1]
                if previous_segment.end_time > segment.start_time:
                    speaker_interruptions[segment_speaker] = (
                        speaker_interruptions.get(segment_speaker, 0) + 1
                    )
                    total_interruptions += 1

            total_words += word_count
            total_talk_time += segment_duration
            previous_speaker = segment_speaker

        # Calculate speaking pace
        speaking_pace = None
        if total_talk_time > 0:
            speaking_pace = (total_words / total_talk_time) * 60  # words per minute

        # Calculate silence ratio
        silence_ratio = None
        if total_duration > 0:
            silence_ratio = max(0, (total_duration - total_talk_time) / total_duration)

        return OverallAnalytics(
            word_count=total_words,
            duration_seconds=total_duration,
            talk_time=SpeakerTimeStats(by_speaker=speaker_times, total=total_talk_time),
            interruptions=InterruptionStats(
                by_speaker=speaker_interruptions, total=total_interruptions
            ),
            turn_taking=TurnTakingStats(by_speaker=speaker_turns, total_turns=len(segments)),
            questions=QuestionStats(by_speaker=speaker_questions, total=total_questions),
            speaking_pace=speaking_pace,
            silence_ratio=silence_ratio,
        )

    @staticmethod
    def _get_analytics_speaker_key(segment: TranscriptSegment) -> str:
        """Get the original speaker key for analytics data consistency.

        This method is specifically for analytics calculations and always returns
        the original SPEAKER_## key format for consistent data structure.

        Args:
            segment: TranscriptSegment object with speaker information.

        Returns:
            Original speaker key (e.g., "SPEAKER_01") or "Unknown"
        """
        if segment.speaker and segment.speaker.name:
            return str(segment.speaker.name)
        else:
            return "Unknown"

    @staticmethod
    def save_analytics(db: Session, media_file_id: int, analytics: OverallAnalytics) -> bool:
        """Save computed analytics to the database.

        This method persists analytics data to the database, handling both
        new records and updates to existing analytics. The analytics are
        stored as JSON in the overall_analytics field.

        Args:
            db: SQLAlchemy database session for persistence operations.
            media_file_id: ID of the media file the analytics belong to.
            analytics: OverallAnalytics object containing computed statistics.

        Returns:
            True if analytics were saved successfully, False if an error occurred.

        Raises:
            Logs errors but does not raise exceptions to prevent disrupting
            the analytics computation workflow.

        Note:
            - Existing analytics are updated with new computed_at timestamp
            - New analytics are created with version "1.0"
            - Database rollback is performed on error
        """
        try:
            # Check if analytics already exist
            existing_analytics = (
                db.query(Analytics).filter(Analytics.media_file_id == media_file_id).first()
            )

            analytics_data = analytics.model_dump()

            if existing_analytics:
                # Update existing analytics
                existing_analytics.overall_analytics = analytics_data  # type: ignore[assignment]
                existing_analytics.computed_at = datetime.now(UTC)  # type: ignore[assignment]
                existing_analytics.version = "1.0"  # type: ignore[assignment]
            else:
                # Create new analytics record
                new_analytics = Analytics(
                    media_file_id=media_file_id,
                    overall_analytics=analytics_data,
                    computed_at=datetime.now(UTC),
                    version="1.0",
                )
                db.add(new_analytics)

            db.commit()
            logger.info(f"Saved analytics for media file {media_file_id}")
            return True

        except Exception as e:
            logger.error(f"Error saving analytics for media file {media_file_id}: {e}")
            db.rollback()
            return False

    @staticmethod
    def refresh_analytics(db: Session, media_file_id: int) -> bool:
        """Regenerate analytics for a media file with updated speaker keys.

        This method forces recomputation of analytics to ensure they use
        the current SPEAKER_## key format instead of old display names.

        Args:
            db: Database session
            media_file_id: ID of the media file

        Returns:
            True if analytics were successfully regenerated, False otherwise
        """
        try:
            # Delete existing analytics first
            existing_analytics = (
                db.query(Analytics).filter(Analytics.media_file_id == media_file_id).first()
            )
            if existing_analytics:
                db.delete(existing_analytics)
                db.commit()

            # Recompute and save new analytics
            return AnalyticsService.compute_and_save_analytics(db, media_file_id)

        except Exception as e:
            logger.error(f"Error regenerating analytics for media file {media_file_id}: {e}")
            db.rollback()
            return False

    @staticmethod
    def compute_and_save_analytics(db: Session, media_file_id: int) -> bool:
        """Compute and save analytics for a media file in one operation.

        This convenience method combines analytics computation and persistence
        into a single atomic operation, ensuring data consistency.

        Args:
            db: SQLAlchemy database session for all operations.
            media_file_id: ID of the media file to analyze and save analytics for.

        Returns:
            True if analytics were computed and saved successfully,
            False if either operation failed.

        Note:
            This method is transactional - if saving fails, no partial
            data is left in an inconsistent state.
        """
        analytics = AnalyticsService.compute_analytics(db, media_file_id)
        if analytics:
            return AnalyticsService.save_analytics(db, media_file_id, analytics)
        return False
