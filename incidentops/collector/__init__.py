"""Deterministic evidence collection for IncidentOps Core.

The Collector prepares safe normalized documents for the Core API. It never
owns chunking, embedding, retrieval, investigation, or database access.
"""

from .service import CollectorService

__all__ = ["CollectorService"]
