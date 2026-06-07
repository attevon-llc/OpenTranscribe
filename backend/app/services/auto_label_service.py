"""
Auto-labeling service for AI-generated tags and collections.

Provides fuzzy deduplication, automatic application of high-confidence
suggestions, batch grouping for multi-file uploads, and retroactive
application to existing files.
"""

import difflib
import logging
import re
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD
from app.core.constants import FUZZY_MATCH_THRESHOLD
from app.core.constants import TAG_SOURCE_AUTO_AI
from app.core.constants import TAG_SOURCE_BULK_GROUP
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.prompt import UserSetting
from app.models.topic import TopicSuggestion

logger = logging.getLogger(__name__)


class AutoLabelService:
    """Service for auto-applying AI-generated tag and collection suggestions."""

    def __init__(self, db: Session):
        self.db = db
        self._tag_cache: Optional[list[Tag]] = None
        self._collection_cache: dict[int, list[Collection]] = {}

    # =========================================================================
    # Name Normalization & Fuzzy Matching
    # =========================================================================

    @staticmethod
    def normalize_name(name: str) -> str:
        """Normalize a name for deduplication comparison.

        Lowercase, strip whitespace, replace hyphens/underscores with spaces,
        collapse multiple spaces.
        """
        if not name:
            return ""
        normalized = name.lower().strip()
        normalized = re.sub(r"[-_]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    @staticmethod
    def are_names_similar(a: str, b: str, threshold: float = FUZZY_MATCH_THRESHOLD) -> bool:
        """Check if two names are similar using SequenceMatcher."""
        norm_a = AutoLabelService.normalize_name(a)
        norm_b = AutoLabelService.normalize_name(b)
        if not norm_a or not norm_b:
            return False
        if norm_a == norm_b:
            return True
        ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
        return ratio >= threshold

    def _get_all_tags_cached(self) -> list[Tag]:
        """Return all tags, using instance-level cache to avoid repeated queries."""
        if self._tag_cache is None:
            self._tag_cache = self.db.query(Tag).all()
        return self._tag_cache

    def _invalidate_tag_cache(self) -> None:
        """Invalidate the tag cache after creating a new tag."""
        self._tag_cache = None

    def _get_user_collections_cached(self, user_id: int) -> list[Collection]:
        """Return user collections, using instance-level cache."""
        if user_id not in self._collection_cache:
            self._collection_cache[user_id] = (
                self.db.query(Collection).filter(Collection.user_id == user_id).all()
            )
        return self._collection_cache[user_id]

    def _invalidate_collection_cache(self, user_id: int) -> None:
        """Invalidate the collection cache for a user after creating a new collection."""
        self._collection_cache.pop(user_id, None)

    def find_existing_similar_tag(self, suggested_name: str) -> Optional[Tag]:
        """Find an existing tag that matches the suggested name.

        1. Exact normalized_name match (uses index)
        2. Fallback: SequenceMatcher scan of cached tags
        """
        normalized = self.normalize_name(suggested_name)

        # Fast path: exact normalized match
        tag: Tag | None = self.db.query(Tag).filter(Tag.normalized_name == normalized).first()
        if tag:
            return tag

        # Slow path: fuzzy scan using cached tag list
        all_tags = self._get_all_tags_cached()
        for existing in all_tags:
            if self.are_names_similar(suggested_name, existing.name):
                return existing
        return None

    def find_existing_similar_collection(
        self, user_id: int, suggested_name: str
    ) -> Optional[Collection]:
        """Find an existing collection matching the suggested name, scoped to user."""
        user_collections = self._get_user_collections_cached(user_id)
        for coll in user_collections:
            if self.are_names_similar(suggested_name, coll.name):
                return coll
        return None

    # =========================================================================
    # Auto-Apply Logic
    # =========================================================================

    def auto_apply_suggestions(
        self,
        media_file: MediaFile,
        suggestion: TopicSuggestion,
        user_id: int,
        confidence_threshold: float = DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD,
        apply_tags: bool = True,
        apply_collections: bool = True,
    ) -> dict:
        """Auto-apply high-confidence suggestions as tags and collections.

        Returns dict with auto_applied_tags, auto_applied_collections,
        skipped_tags, skipped_collections.
        """
        result: dict[str, Any] = {
            "auto_applied_tags": [],
            "auto_applied_collections": [],
            "skipped_tags": [],
            "skipped_collections": [],
        }

        suggested_tags = suggestion.suggested_tags or []
        suggested_collections = suggestion.suggested_collections or []

        # Auto-apply tags
        if apply_tags:
            for tag_data in suggested_tags:
                name = tag_data.get("name", "")
                if not name or not name.strip():
                    result["skipped_tags"].append(name)
                    continue
                confidence = tag_data.get("confidence", 0.0)

                if confidence < confidence_threshold:
                    result["skipped_tags"].append(name)
                    continue

                try:
                    tag = self._get_or_create_tag_with_dedup(name)
                    self._add_tag_to_file(media_file, tag, confidence)
                    result["auto_applied_tags"].append(name)
                except Exception as e:
                    logger.warning(f"Failed to auto-apply tag '{name}': {e}")
                    result["skipped_tags"].append(name)

        # Auto-apply collections
        if apply_collections:
            for coll_data in suggested_collections:
                name = coll_data.get("name", "")
                if not name or not name.strip():
                    result["skipped_collections"].append(name)
                    continue
                confidence = coll_data.get("confidence", 0.0)

                if confidence < confidence_threshold:
                    result["skipped_collections"].append(name)
                    continue

                try:
                    collection = self._get_or_create_collection_with_dedup(name, user_id)
                    self._add_file_to_collection(media_file, collection, confidence)
                    result["auto_applied_collections"].append(name)
                except Exception as e:
                    logger.warning(f"Failed to auto-apply collection '{name}': {e}")
                    result["skipped_collections"].append(name)

        # Update suggestion record
        suggestion.auto_applied_tags = result["auto_applied_tags"]
        suggestion.auto_applied_collections = result["auto_applied_collections"]
        suggestion.auto_apply_completed_at = datetime.now(timezone.utc)

        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        logger.info(
            f"Auto-applied {len(result['auto_applied_tags'])} tags and "
            f"{len(result['auto_applied_collections'])} collections for file {media_file.id}"
        )

        return result

    def _get_or_create_tag_with_dedup(self, name: str, source: str = TAG_SOURCE_AUTO_AI) -> Tag:
        """Get or create a tag, using fuzzy matching to prevent duplicates."""
        existing = self.find_existing_similar_tag(name)
        if existing:
            return existing

        normalized = self.normalize_name(name)
        try:
            nested = self.db.begin_nested()
            tag = Tag(name=name, source=source, normalized_name=normalized)
            self.db.add(tag)
            self.db.flush()
            self._invalidate_tag_cache()
            return tag
        except IntegrityError:
            nested.rollback()
            self._invalidate_tag_cache()
            existing_tag: Optional[Tag] = self.db.query(Tag).filter(Tag.name == name).first()
            if existing_tag:
                return existing_tag
            raise

    def _get_or_create_collection_with_dedup(
        self, name: str, user_id: int, source: str = TAG_SOURCE_AUTO_AI
    ) -> Collection:
        """Get or create a collection, using fuzzy matching to prevent duplicates."""
        existing = self.find_existing_similar_collection(user_id, name)
        if existing:
            return existing

        try:
            nested = self.db.begin_nested()
            collection = Collection(name=name, user_id=user_id, source=source)
            self.db.add(collection)
            self.db.flush()
            self._invalidate_collection_cache(user_id)
            return collection
        except IntegrityError:
            nested.rollback()
            self._invalidate_collection_cache(user_id)
            existing_collection: Optional[Collection] = (
                self.db.query(Collection)
                .filter(Collection.user_id == user_id, Collection.name == name)
                .first()
            )
            if existing_collection:
                return existing_collection
            raise

    def _add_tag_to_file(self, media_file: MediaFile, tag: Tag, confidence: float) -> None:
        """Add a tag to a file if not already present."""
        existing = (
            self.db.query(FileTag)
            .filter(FileTag.media_file_id == media_file.id, FileTag.tag_id == tag.id)
            .first()
        )
        if existing:
            return

        try:
            nested = self.db.begin_nested()
            file_tag = FileTag(
                media_file_id=media_file.id,
                tag_id=tag.id,
                source=TAG_SOURCE_AUTO_AI,
                ai_confidence=confidence,
            )
            self.db.add(file_tag)
            self.db.flush()
            # Bust the owning user's read-through tag cache (auto-labeling is a
            # back-door tag-mutation path that doesn't carry current_user).
            try:
                from app.services.redis_cache_service import redis_cache

                redis_cache.invalidate_tags(int(media_file.user_id))
                redis_cache.invalidate_user_files(int(media_file.user_id))
            except Exception as e:
                logger.debug(f"Tag cache invalidation failed (non-critical): {e}")
        except IntegrityError:
            nested.rollback()
            logger.debug(f"Duplicate file_tag for file={media_file.id} tag={tag.id}, skipping")

    def _add_file_to_collection(
        self, media_file: MediaFile, collection: Collection, confidence: float
    ) -> None:
        """Add a file to a collection if not already a member."""
        existing = (
            self.db.query(CollectionMember)
            .filter(
                CollectionMember.collection_id == collection.id,
                CollectionMember.media_file_id == media_file.id,
            )
            .first()
        )
        if existing:
            return

        try:
            nested = self.db.begin_nested()
            member = CollectionMember(
                collection_id=collection.id,
                media_file_id=media_file.id,
                source=TAG_SOURCE_AUTO_AI,
                ai_confidence=confidence,
            )
            self.db.add(member)
            self.db.flush()
        except IntegrityError:
            nested.rollback()
            logger.debug(
                f"Duplicate collection_member for file={media_file.id} "
                f"collection={collection.id}, skipping"
            )

    # =========================================================================
    # Batch Grouping
    # =========================================================================

    def group_batch_by_topics(self, batch_id: int, user_id: int) -> dict:
        """Create shared collections for files in a batch that share topics.

        After all files in a batch complete topic extraction:
        1. Collect all suggested tags from batch files
        2. Normalize and cluster by similarity
        3. For tags appearing in 2+ files, create a shared collection
        """
        from app.models.upload_batch import UploadBatch as UploadBatchModel

        batch = self.db.query(UploadBatchModel).filter(UploadBatchModel.id == batch_id).first()
        if not batch:
            return {"collections_created": 0, "files_grouped": 0}

        # Get all files in batch with their suggestions (batch-fetch, no N+1)
        batch_files = self.db.query(MediaFile).filter(MediaFile.upload_batch_id == batch_id).all()
        batch_file_ids = [mf.id for mf in batch_files]
        suggestions_batch = (
            self.db.query(TopicSuggestion)
            .filter(TopicSuggestion.media_file_id.in_(batch_file_ids))
            .all()
            if batch_file_ids
            else []
        )
        suggestion_by_file = {s.media_file_id: s for s in suggestions_batch}

        # Collect tag->files mapping
        tag_files: dict[str, list[MediaFile]] = {}
        for mf in batch_files:
            suggestion = suggestion_by_file.get(mf.id)
            if not suggestion or not suggestion.suggested_tags:
                continue
            for tag_data in suggestion.suggested_tags:
                normalized = self.normalize_name(tag_data.get("name", ""))
                if not normalized:
                    continue
                if normalized not in tag_files:
                    tag_files[normalized] = []
                if mf not in tag_files[normalized]:
                    tag_files[normalized].append(mf)

        # Create collections for shared topics (2+ files)
        collections_created = 0
        files_grouped = set()

        for tag_name, files in tag_files.items():
            if len(files) < 2:
                continue

            # Create collection name from tag
            collection_name = tag_name.title()

            # _get_or_create_collection_with_dedup already does fuzzy lookup
            # internally; detect new collections via cache invalidation.
            # Ensure cache is populated so we can detect invalidation.
            self._get_user_collections_cached(user_id)
            collection = self._get_or_create_collection_with_dedup(
                collection_name, user_id, source=TAG_SOURCE_BULK_GROUP
            )

            for mf in files:
                self._add_file_to_collection(mf, collection, confidence=0.0)
                files_grouped.add(mf.id)

            # Cache is invalidated only when a new collection is created
            if user_id not in self._collection_cache:
                collections_created += 1

        batch.grouping_status = "completed"
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return {
            "collections_created": collections_created,
            "files_grouped": len(files_grouped),
        }

    # =========================================================================
    # Retroactive Apply
    # =========================================================================

    def retroactive_apply(
        self,
        user_id: int,
        confidence_threshold: float = DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD,
        file_ids: Optional[list[int]] = None,
        progress_callback=None,
    ) -> dict:
        """Apply auto-labeling to existing files with pending suggestions.

        Args:
            user_id: User ID
            confidence_threshold: Minimum confidence for auto-apply
            file_ids: Optional list of specific file IDs to process
            progress_callback: Optional callback(processed, total, file_name)
        """
        query = self.db.query(TopicSuggestion).filter(
            TopicSuggestion.user_id == user_id,
            TopicSuggestion.auto_apply_completed_at.is_(None),
        )

        if file_ids:
            query = query.filter(TopicSuggestion.media_file_id.in_(file_ids))

        # Collect IDs upfront so we can re-query each suggestion fresh
        # inside the loop.  After a rollback() ORM objects loaded from the
        # original query may be stale/detached.
        suggestion_ids = [s.id for s in query.all()]

        result: dict[str, Any] = {
            "files_processed": 0,
            "tags_applied": 0,
            "collections_applied": 0,
            "files_skipped": 0,
            "errors": [],
        }

        total = len(suggestion_ids)
        settings = self.get_user_auto_label_settings(user_id)
        apply_tags = settings.get("tags_enabled", True)
        apply_collections = settings.get("collections_enabled", True)

        for i, suggestion_id in enumerate(suggestion_ids):
            try:
                # Re-query suggestion each iteration to avoid stale objects
                suggestion = (
                    self.db.query(TopicSuggestion)
                    .filter(TopicSuggestion.id == suggestion_id)
                    .first()
                )
                if not suggestion:
                    result["files_skipped"] += 1
                    continue

                media_file = (
                    self.db.query(MediaFile)
                    .filter(MediaFile.id == suggestion.media_file_id)
                    .first()
                )
                if not media_file:
                    result["files_skipped"] += 1
                    continue

                apply_result = self.auto_apply_suggestions(
                    media_file=media_file,
                    suggestion=suggestion,
                    user_id=user_id,
                    confidence_threshold=confidence_threshold,
                    apply_tags=apply_tags,
                    apply_collections=apply_collections,
                )

                result["files_processed"] += 1
                result["tags_applied"] += len(apply_result["auto_applied_tags"])
                result["collections_applied"] += len(apply_result["auto_applied_collections"])

                if progress_callback:
                    progress_callback(
                        i + 1,
                        total,
                        media_file.filename,
                        file_uuid=str(media_file.uuid),
                    )

            except Exception as e:
                logger.error(f"Error processing suggestion {suggestion_id}: {e}")
                result["errors"].append(str(e))
                result["files_skipped"] += 1

        return result

    # =========================================================================
    # User Settings
    # =========================================================================

    def get_user_auto_label_settings(self, user_id: int) -> dict:
        """Read auto-label settings from user_setting table."""
        defaults = {
            "enabled": True,
            "confidence_threshold": DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD,
            "tags_enabled": True,
            "collections_enabled": True,
            "bulk_grouping_enabled": True,
        }

        setting_keys: dict[str, tuple[str, Any]] = {
            "auto_label_enabled": ("enabled", lambda v: v.lower() == "true"),
            "auto_label_confidence_threshold": ("confidence_threshold", float),
            "auto_label_tags_enabled": ("tags_enabled", lambda v: v.lower() == "true"),
            "auto_label_collections_enabled": (
                "collections_enabled",
                lambda v: v.lower() == "true",
            ),
            "auto_label_bulk_grouping_enabled": (
                "bulk_grouping_enabled",
                lambda v: v.lower() == "true",
            ),
        }

        settings = (
            self.db.query(UserSetting)
            .filter(
                UserSetting.user_id == user_id,
                UserSetting.setting_key.in_(setting_keys.keys()),
            )
            .all()
        )

        result = dict(defaults)
        for setting in settings:
            key_info = setting_keys.get(setting.setting_key)
            if key_info:
                result_key, converter = key_info
                try:
                    result[result_key] = converter(setting.setting_value)
                except (ValueError, AttributeError):
                    logger.debug(f"Invalid value for setting {setting.setting_key}, using default")

        return result

    def save_user_auto_label_settings(self, user_id: int, settings_data: dict) -> None:
        """Save auto-label settings to user_setting table.

        Validates confidence_threshold is between 0.0 and 1.0 before saving.

        Raises:
            ValueError: If confidence_threshold is out of range.
        """
        # Validate confidence_threshold range
        if "confidence_threshold" in settings_data:
            try:
                threshold = float(settings_data["confidence_threshold"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "confidence_threshold must be a number between 0.0 and 1.0"
                ) from exc
            if threshold < 0.0 or threshold > 1.0:
                raise ValueError(
                    f"confidence_threshold must be between 0.0 and 1.0, got {threshold}"
                )

        key_map = {
            "enabled": "auto_label_enabled",
            "confidence_threshold": "auto_label_confidence_threshold",
            "tags_enabled": "auto_label_tags_enabled",
            "collections_enabled": "auto_label_collections_enabled",
            "bulk_grouping_enabled": "auto_label_bulk_grouping_enabled",
        }

        for field, db_key in key_map.items():
            if field not in settings_data:
                continue
            value = (
                str(settings_data[field]).lower()
                if isinstance(settings_data[field], bool)
                else str(settings_data[field])
            )

            existing = (
                self.db.query(UserSetting)
                .filter(UserSetting.user_id == user_id, UserSetting.setting_key == db_key)
                .first()
            )
            if existing:
                existing.setting_value = value
            else:
                new_setting = UserSetting(
                    user_id=user_id,
                    setting_key=db_key,
                    setting_value=value,
                )
                self.db.add(new_setting)

        self.db.commit()
