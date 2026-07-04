"""
OpenSearch Summary Service

Handles indexing, searching, and analytics for AI-generated summaries
stored in OpenSearch for advanced search capabilities.
"""

import datetime
import logging
import uuid
from typing import Any

from opensearchpy.exceptions import NotFoundError

from app.core.config import settings
from app.services.opensearch_service import opensearch_client

logger = logging.getLogger(__name__)


class OpenSearchSummaryService:
    """Service for managing summaries in OpenSearch"""

    def __init__(self):
        self.client = opensearch_client
        self.index_name = getattr(settings, "OPENSEARCH_SUMMARY_INDEX", "transcript_summaries")

        # Ensure the summary index exists
        self._ensure_summary_index_exists()

    def _ensure_summary_index_exists(self):
        """
        Create the summary index with dynamic mapping to support flexible summary structures.

        This approach allows any JSON structure from custom AI prompts while still
        providing full-text search and analytics capabilities.
        """
        if not self.client:
            logger.warning("OpenSearch client not initialized")
            return

        try:
            if not self.client.indices.exists(index=self.index_name):
                # Define flexible mapping that accepts any summary structure
                index_config = {
                    "settings": {
                        "index": {
                            "number_of_shards": 1,
                            "number_of_replicas": 0,
                            "max_result_window": 10000,  # Default pagination limit
                        },
                        "analysis": {
                            "analyzer": {
                                "summary_analyzer": {
                                    "type": "standard",
                                    "stopwords": ["_english_"],
                                }
                            }
                        },
                    },
                    "mappings": {
                        "dynamic": True,  # Allow new fields from custom prompts
                        "properties": {
                            # Core tracking fields (always present)
                            "file_id": {"type": "integer"},
                            "user_id": {"type": "integer"},
                            "summary_version": {"type": "integer"},
                            "provider": {"type": "keyword"},
                            "model": {"type": "keyword"},
                            "created_at": {"type": "date"},
                            "updated_at": {"type": "date"},
                            # Flexible summary content - stores complete JSON structure
                            # Disabled indexing prevents type conflicts between different prompt formats
                            # All searchable text is extracted to searchable_content field
                            "summary_content": {
                                "type": "object",
                                "enabled": False,  # Store but don't index nested fields
                            },
                            # Full-text searchable combined content (extracted from all text fields)
                            "searchable_content": {
                                "type": "text",
                                "analyzer": "summary_analyzer",
                            },
                        },
                    },
                }

                self.client.indices.create(index=self.index_name, body=index_config)

                logger.info(f"Created flexible summary index: {self.index_name}")

        except Exception as e:
            logger.error(f"Error creating summary index: {e}")

    async def index_summary(self, summary_data: dict[str, Any]) -> str | None:
        """
        Index a summary document in OpenSearch with flexible structure support.

        Args:
            summary_data: Summary data dictionary (any JSON structure)

        Returns:
            Document ID of the indexed summary
        """
        if not self.client:
            logger.warning("OpenSearch client not initialized")
            return None

        try:
            # Generate a unique document ID
            doc_id = str(uuid.uuid4())

            # Extract core tracking fields before preparing document
            file_id = summary_data.get("file_id")
            user_id = summary_data.get("user_id")
            summary_version = summary_data.get("summary_version", 1)
            provider = summary_data.get("provider", "unknown")
            model = summary_data.get("model", "unknown")

            # Create a clean copy WITHOUT tracking fields for summary_content storage
            clean_summary_data = {
                k: v
                for k, v in summary_data.items()
                if k not in ("file_id", "user_id", "summary_version", "provider", "model")
            }

            # Prepare the document with flexible structure (without tracking fields)
            doc = self._prepare_summary_document(clean_summary_data)

            # Add core tracking fields at root level (not in summary_content)
            doc["file_id"] = file_id
            doc["user_id"] = user_id
            doc["summary_version"] = summary_version
            doc["provider"] = provider
            doc["model"] = model
            doc["created_at"] = datetime.datetime.now(datetime.UTC).isoformat()
            doc["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

            # Index the document
            self.client.index(
                index=self.index_name,
                id=doc_id,
                body=doc,
                refresh=True,  # Make document immediately searchable
            )

            logger.info(f"Indexed flexible summary for file {file_id}: {doc_id}")
            return doc_id

        except Exception as e:
            logger.error(f"Error indexing summary: {e}")
            logger.error(
                f"Summary data keys: {list(summary_data.keys()) if summary_data else 'None'}"
            )
            return None

    async def get_summary(self, document_id: str) -> dict[str, Any] | None:
        """
        Retrieve a summary document by ID with flexible structure.

        Args:
            document_id: OpenSearch document ID

        Returns:
            Summary document with summary_content extracted, or None if not found
        """
        if not self.client:
            return None

        try:
            response = self.client.get(index=self.index_name, id=document_id)
            source = response["_source"]

            # Extract the flexible summary_content and merge with metadata
            summary_content = source.get("summary_content", {})
            metadata = source.get("metadata", {})

            # Reconstruct the full summary with metadata
            full_summary = {**summary_content, "metadata": metadata}

            return full_summary

        except NotFoundError:
            logger.warning(f"Summary document not found: {document_id}")
            return None
        except Exception as e:
            logger.error(f"Error retrieving summary: {e}")
            return None

    async def get_summary_by_file_id(self, file_id: int, user_id: int) -> dict[str, Any] | None:
        """
        Get the latest summary for a specific file with flexible structure support.

        Args:
            file_id: Media file ID
            user_id: User ID for security

        Returns:
            Latest summary document or None if not found
        """
        if not self.client:
            return None

        try:
            # Search for summaries for this file, get the latest version
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"file_id": file_id}},
                            {"term": {"user_id": user_id}},
                        ]
                    }
                },
                "sort": [
                    {"summary_version": {"order": "desc"}},
                    {"created_at": {"order": "desc"}},
                ],
                "size": 1,
            }

            response = self.client.search(index=self.index_name, body=query)

            if response["hits"]["hits"]:
                hit = response["hits"]["hits"][0]
                source = hit["_source"]

                # Extract the flexible summary_content and merge with metadata
                summary_content = source.get("summary_content", {})
                metadata = source.get("metadata", {})

                # Reconstruct the full summary with metadata (user-facing content only)
                full_summary = {**summary_content, "metadata": metadata}

                # Return only user-facing data, filtering out OpenSearch tracking fields
                # (file_id, user_id, summary_version, provider, model)
                return {
                    "document_id": hit["_id"],
                    "summary_data": full_summary,
                    "created_at": source.get("created_at"),
                    "updated_at": source.get("updated_at"),
                }

            return None

        except Exception as e:
            logger.error(f"Error getting summary for file {file_id}: {e}")
            return None

    async def search_summaries(
        self, query: dict[str, Any], user_id: int, size: int = 20, from_: int = 0
    ) -> dict[str, Any]:
        """
        Search across all summaries with complex queries

        Args:
            query: Search query parameters
            user_id: User ID for security filtering
            size: Number of results to return
            from_: Offset for pagination

        Returns:
            Search results with metadata
        """
        if not self.client:
            return {"hits": [], "total": 0}

        try:
            # Base query - always filter by user
            search_body = {
                "query": {
                    "bool": {
                        "must": [{"term": {"user_id": user_id}}],
                        "should": [],
                        "filter": [],
                    }
                },
                "size": size,
                "from": from_,
                "sort": [{"created_at": {"order": "desc"}}],
                "highlight": {
                    "fields": {
                        "bluf": {},
                        "brief_summary": {},
                        "searchable_content": {"fragment_size": 200},
                    },
                    "pre_tags": ["<mark>"],
                    "post_tags": ["</mark>"],
                },
            }

            # Add text search if provided
            if query.get("text"):
                text_query = {
                    "multi_match": {
                        "query": query["text"],
                        "fields": [
                            "bluf^3",  # Boost BLUF content
                            "brief_summary^2",
                            "searchable_content",
                            "major_topics.topic^2",
                            "major_topics.key_points",
                            "action_items.text",
                            "key_decisions^2",
                        ],
                        "fuzziness": "AUTO",
                    }
                }
                search_body["query"]["bool"]["should"].append(text_query)  # type: ignore[index]
                search_body["query"]["bool"]["minimum_should_match"] = 1  # type: ignore[index]

            # Add date range filter
            if query.get("date_from") or query.get("date_to"):
                date_filter: dict[str, Any] = {"range": {"created_at": {}}}
                if query.get("date_from"):
                    date_filter["range"]["created_at"]["gte"] = query["date_from"]
                if query.get("date_to"):
                    date_filter["range"]["created_at"]["lte"] = query["date_to"]
                search_body["query"]["bool"]["filter"].append(date_filter)  # type: ignore[index]

            # Add action item filter
            if query.get("has_pending_actions"):
                action_filter = {
                    "nested": {
                        "path": "action_items",
                        "query": {
                            "bool": {"must_not": {"term": {"action_items.status": "completed"}}}
                        },
                    }
                }
                search_body["query"]["bool"]["filter"].append(action_filter)  # type: ignore[index]

            # Execute search
            response = self.client.search(index=self.index_name, body=search_body)

            # Process results
            hits = []
            for hit in response["hits"]["hits"]:
                source = hit["_source"]
                result = {
                    "document_id": hit["_id"],
                    "score": hit["_score"],
                    "file_id": source["file_id"],
                    "bluf": source.get("bluf", ""),
                    "brief_summary": source.get("brief_summary", ""),
                    "created_at": source.get("created_at"),
                    "provider": source.get("provider"),
                    "model": source.get("model"),
                }

                # Add highlights if available
                if "highlight" in hit:
                    result["highlights"] = hit["highlight"]

                hits.append(result)

            return {
                "hits": hits,
                "total": response["hits"]["total"]["value"],
                "max_score": response["hits"]["max_score"],
            }

        except Exception as e:
            logger.error(f"Error searching summaries: {e}")
            return {"hits": [], "total": 0}

    async def get_summary_analytics(
        self, user_id: int, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Get analytics and aggregations for summaries

        Args:
            user_id: User ID
            filters: Optional filters to apply

        Returns:
            Analytics data
        """
        if not self.client:
            return {}

        try:
            # Base query with user filter
            agg_query = {
                "size": 0,  # We only want aggregations
                "query": {"bool": {"must": [{"term": {"user_id": user_id}}]}},
                "aggs": {
                    # Action items over time
                    "action_items_trend": {
                        "date_histogram": {
                            "field": "created_at",
                            "calendar_interval": "week",
                        },
                        "aggs": {
                            "action_count": {
                                "nested": {"path": "action_items"},
                                "aggs": {
                                    "total": {"value_count": {"field": "action_items.text.keyword"}}
                                },
                            },
                            "pending_actions": {
                                "nested": {"path": "action_items"},
                                "aggs": {
                                    "pending": {
                                        "filter": {
                                            "bool": {
                                                "must_not": {
                                                    "term": {"action_items.status": "completed"}
                                                }
                                            }
                                        }
                                    }
                                },
                            },
                        },
                    },
                    # Most common topics
                    "common_topics": {
                        "nested": {"path": "major_topics"},
                        "aggs": {
                            "topics": {
                                "terms": {
                                    "field": "major_topics.topic.keyword",
                                    "size": 15,
                                }
                            }
                        },
                    },
                    # Summary statistics
                    "summary_stats": {"stats": {"field": "metadata.transcript_length"}},
                    # Provider usage
                    "provider_usage": {"terms": {"field": "provider", "size": 10}},
                },
            }

            # Apply date filter if provided
            if filters and filters.get("date_from"):
                date_filter = {"range": {"created_at": {"gte": filters["date_from"]}}}
                if filters.get("date_to"):
                    date_filter["range"]["created_at"]["lte"] = filters["date_to"]
                agg_query["query"]["bool"]["must"].append(date_filter)  # type: ignore[index]

            response = self.client.search(index=self.index_name, body=agg_query)

            # Process aggregations
            aggs = response["aggregations"]

            analytics = {
                "total_summaries": response["hits"]["total"]["value"],
                "speaker_stats": [],  # No longer tracking speaker stats
                "action_items_trend": self._process_trend_aggregation(aggs["action_items_trend"]),
                "common_topics": self._process_topics_aggregation(aggs["common_topics"]),
                "summary_statistics": aggs["summary_stats"],
                "provider_usage": self._process_terms_aggregation(aggs["provider_usage"]),
            }

            return analytics

        except Exception as e:
            logger.error(f"Error getting summary analytics: {e}")
            return {}

    async def update_summary(self, document_id: str, updates: dict[str, Any]) -> bool:
        """
        Update a summary document

        Args:
            document_id: OpenSearch document ID
            updates: Fields to update

        Returns:
            Success status
        """
        if not self.client:
            return False

        try:
            # Add updated timestamp
            updates["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

            self.client.update(
                index=self.index_name,
                id=document_id,
                body={"doc": updates},
                refresh=True,
            )

            logger.info(f"Updated summary document: {document_id}")
            return True

        except Exception as e:
            logger.error(f"Error updating summary: {e}")
            return False

    async def get_max_version(self, file_id: int, user_id: int) -> int:
        """
        Get the highest version number for a file's summaries

        Args:
            file_id: Media file ID
            user_id: User ID for security

        Returns:
            Maximum version number for the file, or 0 if no summaries exist
        """
        if not self.client:
            return 0

        try:
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"file_id": file_id}},
                            {"term": {"user_id": user_id}},
                        ]
                    }
                },
                "aggs": {"max_version": {"max": {"field": "summary_version"}}},
                "size": 0,
            }

            response = self.client.search(index=self.index_name, body=query)
            max_version = response["aggregations"]["max_version"]["value"]
            return int(max_version) if max_version else 0

        except Exception as e:
            logger.error(f"Failed to get max version for file {file_id}: {e}")
            return 0

    async def delete_summary(self, document_id: str) -> bool:
        """
        Delete a summary document

        Args:
            document_id: OpenSearch document ID

        Returns:
            Success status
        """
        if not self.client:
            return False

        try:
            self.client.delete(index=self.index_name, id=document_id, refresh=True)

            logger.info(f"Deleted summary document: {document_id}")
            return True

        except NotFoundError:
            logger.warning(f"Summary document not found for deletion: {document_id}")
            return False
        except Exception as e:
            logger.error(f"Error deleting summary: {e}")
            return False

    def _prepare_summary_document(self, summary_data: dict[str, Any]) -> dict[str, Any]:
        """
        Prepare summary data for indexing with flexible structure support.

        Extracts all text content recursively for full-text search while
        preserving the complete JSON structure in summary_content field.

        Args:
            summary_data: Raw summary data (any JSON structure)

        Returns:
            Processed document ready for indexing
        """

        def extract_text_recursively(obj: Any, collected_text: list[str]) -> None:
            """
            Recursively extract all text values from any JSON structure.

            This allows full-text search to work regardless of the summary format.
            """
            if obj is None:
                return
            elif isinstance(obj, str):
                if obj.strip():  # Only add non-empty strings
                    collected_text.append(obj)
            elif isinstance(obj, dict):
                # Skip metadata to avoid polluting search with technical details
                for key, value in obj.items():
                    if key != "metadata":
                        extract_text_recursively(value, collected_text)
            elif isinstance(obj, list):
                for item in obj:
                    extract_text_recursively(item, collected_text)
            # Skip numbers, booleans, etc.

        # Extract all text content for searchable_content
        searchable_parts: list[str] = []
        extract_text_recursively(summary_data, searchable_parts)

        # Separate metadata and summary content
        metadata = summary_data.pop("metadata", {})

        # Create the document with flexible structure
        doc = {
            # Store complete summary in flexible field
            "summary_content": summary_data,
            # Create searchable text index
            "searchable_content": " ".join(filter(None, searchable_parts)),
            # Preserve metadata at root level for filtering
            "metadata": metadata,
        }

        return doc

    def _process_trend_aggregation(self, agg: dict[str, Any]) -> list[dict[str, Any]]:
        """Process time-based trend aggregation"""
        trends = []
        for bucket in agg["buckets"]:
            trends.append(
                {
                    "date": bucket["key_as_string"],
                    "total_actions": bucket["action_count"]["total"]["value"],
                    "pending_actions": bucket["pending_actions"]["pending"]["doc_count"],
                }
            )
        return trends

    def _process_topics_aggregation(self, agg: dict[str, Any]) -> list[dict[str, Any]]:
        """Process topics aggregation"""
        topics = []
        for bucket in agg["topics"]["buckets"]:
            topics.append({"topic": bucket["key"], "count": bucket["doc_count"]})
        return topics

    def _process_terms_aggregation(self, agg: dict[str, Any]) -> list[dict[str, Any]]:
        """Process simple terms aggregation"""
        terms = []
        for bucket in agg["buckets"]:
            terms.append({"term": bucket["key"], "count": bucket["doc_count"]})
        return terms
