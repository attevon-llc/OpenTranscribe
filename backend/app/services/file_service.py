"""
File service layer for centralized file operations.

This service provides a high-level interface for file-related operations,
abstracting away the complexity of direct database and storage interactions.
"""

import logging
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.api.endpoints.files import process_file_upload
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.media import MediaFileUpdate
from app.utils.auth_decorators import AuthorizationHelper
from app.utils.db_helpers import add_tags_to_file
from app.utils.db_helpers import get_file_tags
from app.utils.db_helpers import get_user_file_stats
from app.utils.db_helpers import get_user_files_query
from app.utils.db_helpers import remove_tags_from_file
from app.utils.db_helpers import safe_get_by_id
from app.utils.error_handlers import ErrorHandler

logger = logging.getLogger(__name__)


class FileService:
    """Service class for file operations."""

    def __init__(self, db: Session):
        self.db = db

    async def upload_file(self, file: UploadFile, user: User) -> MediaFile:
        """
        Upload a new file.

        Args:
            file: Uploaded file
            user: Current user

        Returns:
            Created MediaFile object
        """
        try:
            return await process_file_upload(file, self.db, user)
        except Exception as e:
            logger.error(f"Error uploading file: {e}")
            raise ErrorHandler.file_processing_error("upload", e) from e

    def get_user_files(self, user: User, filters: dict[str, Any] | None = None) -> list[MediaFile]:
        """
        Get files for a user with optional filtering.

        Args:
            user: Current user
            filters: Optional filters dictionary

        Returns:
            List of MediaFile objects
        """
        try:
            query = get_user_files_query(self.db, int(user.id))

            if filters:
                from app.api.endpoints.files import apply_all_filters

                query = apply_all_filters(query, filters)

            return query.order_by(MediaFile.upload_time.desc()).all()  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Error getting user files: {e}")
            raise ErrorHandler.database_error("file retrieval", e) from e

    def get_file_by_id(self, file_id: int, user: User) -> MediaFile:
        """
        Get a specific file by ID, ensuring user ownership.

        Args:
            file_id: File ID
            user: Current user

        Returns:
            MediaFile object

        Raises:
            HTTPException: If file not found or access denied
        """
        return AuthorizationHelper.check_file_access(self.db, file_id, user)

    def update_file_metadata(self, file_id: int, updates: MediaFileUpdate, user: User) -> MediaFile:
        """
        Update file metadata.

        Args:
            file_id: File ID
            updates: Update data
            user: Current user

        Returns:
            Updated MediaFile object
        """
        try:
            file_obj = self.get_file_by_id(file_id, user)

            # Apply updates
            for field, value in updates.model_dump(exclude_unset=True).items():
                setattr(file_obj, field, value)

            self.db.commit()
            self.db.refresh(file_obj)

            return file_obj
        except Exception as e:
            logger.error(f"Error updating file metadata: {e}")
            self.db.rollback()
            raise ErrorHandler.database_error("file update", e) from e

    def delete_file(self, file_id: int, user: User) -> None:
        """
        Delete a file and all associated data.

        Args:
            file_id: File ID
            user: Current user
        """
        try:
            file_obj = self.get_file_by_id(file_id, user)

            # Delete from storage
            try:
                from app.services.minio_service import delete_file

                delete_file(str(file_obj.storage_path))
            except Exception as storage_error:
                logger.warning(f"Error deleting file from storage: {storage_error}")
                # Continue with database deletion

            # Delete from database
            self.db.delete(file_obj)
            self.db.commit()

        except Exception as e:
            logger.error(f"Error deleting file: {e}")
            self.db.rollback()
            raise ErrorHandler.database_error("file deletion", e) from e

    def get_file_tags(self, file_id: int, user: User) -> list[str]:
        """
        Get tags for a file.

        Args:
            file_id: File ID
            user: Current user

        Returns:
            List of tag names
        """
        # Verify user access
        self.get_file_by_id(file_id, user)
        return get_file_tags(self.db, file_id)

    def add_file_tags(self, file_id: int, tag_names: list[str], user: User) -> bool:
        """
        Add tags to a file.

        Args:
            file_id: File ID
            tag_names: List of tag names to add
            user: Current user

        Returns:
            True if successful
        """
        # Verify user access
        self.get_file_by_id(file_id, user)
        return add_tags_to_file(self.db, file_id, tag_names)

    def remove_file_tags(self, file_id: int, tag_names: list[str], user: User) -> bool:
        """
        Remove tags from a file.

        Args:
            file_id: File ID
            tag_names: List of tag names to remove
            user: Current user

        Returns:
            True if successful
        """
        # Verify user access
        self.get_file_by_id(file_id, user)
        return remove_tags_from_file(self.db, file_id, tag_names)

    def get_user_statistics(self, user: User) -> dict[str, Any]:
        """
        Get comprehensive statistics for a user's files.

        Args:
            user: Current user

        Returns:
            Dictionary with file statistics
        """
        return get_user_file_stats(self.db, int(user.id))

    def get_files_by_status(self, user: User, status: FileStatus) -> list[MediaFile]:
        """
        Get files by status for a user.

        Args:
            user: Current user
            status: File status

        Returns:
            List of MediaFile objects
        """
        try:
            return (  # type: ignore[no-any-return]
                self.db.query(MediaFile)
                .filter(MediaFile.user_id == user.id, MediaFile.status == status)
                .all()
            )
        except Exception as e:
            logger.error(f"Error getting files by status: {e}")
            raise ErrorHandler.database_error("status filtering", e) from e

    def update_file_status(
        self, file_id: int, status: FileStatus, user: User | None = None
    ) -> None:
        """
        Update file status.

        Args:
            file_id: File ID
            status: New status
            user: Current user (optional for system updates)
        """
        try:
            if user:
                file_obj = self.get_file_by_id(file_id, user)
            else:
                file_obj_result = safe_get_by_id(self.db, MediaFile, file_id)
                if not file_obj_result:
                    raise ErrorHandler.not_found_error("File")
                file_obj = file_obj_result

            file_obj.status = status  # type: ignore[assignment]
            self.db.commit()

        except Exception as e:
            logger.error(f"Error updating file status: {e}")
            self.db.rollback()
            raise ErrorHandler.database_error("status update", e) from e

    def search_files(self, user: User, query: str, limit: int = 50) -> list[MediaFile]:
        """
        Search files by filename and content.

        Args:
            user: Current user
            query: Search query
            limit: Maximum number of results

        Returns:
            List of matching MediaFile objects
        """
        try:
            # Search in filename, title, and transcript content
            files_query = self.db.query(MediaFile).filter(MediaFile.user_id == user.id)

            # Apply text search filters
            from app.api.endpoints.files.filtering import apply_search_filter
            from app.api.endpoints.files.filtering import apply_transcript_search_filter

            files_query = apply_search_filter(files_query, query)

            # Also search in transcript content
            transcript_query = self.db.query(MediaFile).filter(MediaFile.user_id == user.id)
            transcript_query = apply_transcript_search_filter(transcript_query, query)

            # Combine results and remove duplicates
            filename_results = files_query.limit(limit // 2).all()
            transcript_results = transcript_query.limit(limit // 2).all()

            # Merge and deduplicate
            all_results = filename_results + transcript_results
            seen_ids = set()
            unique_results = []
            for file_obj in all_results:
                if file_obj.id not in seen_ids:
                    seen_ids.add(file_obj.id)
                    unique_results.append(file_obj)

            return unique_results[:limit]

        except Exception as e:
            logger.error(f"Error searching files: {e}")
            raise ErrorHandler.database_error("file search", e) from e
