"""Regulation knowledge pipeline for mortgage lending compliance.

This package provides:
- Document ingestion from approved regulatory sources
- Embedding-based semantic search over regulation passages
- Change detection and approval workflow for compliance

Eligibility decisions still come from structured rules in mortgage.json.
Search results are for informational context and citations only.
"""

from mortgage.knowledge.index import KnowledgeIndex, Passage, search

__all__ = ["KnowledgeIndex", "Passage", "search"]
