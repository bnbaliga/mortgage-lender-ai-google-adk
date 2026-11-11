"""Embedding index for semantic search over regulation passages.

Uses Gemini embedding model via google-genai, with SQLite + numpy storage.
Interface is designed to be swappable for Vertex AI Search later.

Usage:
    from mortgage.knowledge import search
    results = search("Texas HELOC restrictions", jurisdiction="TX", k=3)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("mortgage.knowledge.index")

# Paths
KNOWLEDGE_DIR = Path(__file__).parent
DB_PATH = KNOWLEDGE_DIR / "passages.db"

# Embedding dimension for Gemini text-embedding-004
EMBEDDING_DIM = 768


@dataclass
class Passage:
    """A searchable passage with metadata."""
    id: int
    source_id: str
    content: str
    title: str
    section: str
    url: Optional[str]
    score: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dict for tool response."""
        return {
            "source_id": self.source_id,
            "title": self.title,
            "section": self.section,
            "content": self.content[:500] + "..." if len(self.content) > 500 else self.content,
            "url": self.url,
            "relevance_score": round(self.score, 3)
        }


def get_embedding(text: str, model: str = "text-embedding-004") -> Optional[np.ndarray]:
    """Get embedding vector for text using Gemini embedding model.

    Returns None if embedding fails (no API key, rate limit, etc.)
    """
    try:
        import google.generativeai as genai

        result = genai.embed_content(
            model=f"models/{model}",
            content=text,
            task_type="retrieval_document"
        )
        return np.array(result["embedding"], dtype=np.float32)
    except ImportError:
        log.warning("google-generativeai not installed")
        return None
    except Exception as e:
        log.warning("Embedding failed: %s", e)
        return None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class KnowledgeIndex:
    """SQLite + numpy index for passage search.

    Interface designed to be swappable for Vertex AI Search.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._embeddings_cache: dict[int, np.ndarray] = {}

    @property
    def conn(self) -> sqlite3.Connection:
        """Lazy database connection."""
        if self._conn is None:
            if not self.db_path.exists():
                raise FileNotFoundError(f"Knowledge database not found: {self.db_path}")
            self._conn = sqlite3.connect(str(self.db_path))
        return self._conn

    def _load_embeddings(self) -> None:
        """Load all approved embeddings into memory."""
        if self._embeddings_cache:
            return

        rows = self.conn.execute("""
            SELECT id, embedding FROM passages
            WHERE status = 'approved' AND embedding IS NOT NULL
        """).fetchall()

        for row_id, embedding_blob in rows:
            if embedding_blob:
                self._embeddings_cache[row_id] = np.frombuffer(embedding_blob, dtype=np.float32)

        log.info("Loaded %d embeddings into cache", len(self._embeddings_cache))

    def embed_passage(self, passage_id: int) -> bool:
        """Embed a single passage and store in database."""
        row = self.conn.execute(
            "SELECT content FROM passages WHERE id = ?", (passage_id,)
        ).fetchone()

        if not row:
            return False

        embedding = get_embedding(row[0])
        if embedding is None:
            return False

        self.conn.execute(
            "UPDATE passages SET embedding = ? WHERE id = ?",
            (embedding.tobytes(), passage_id)
        )
        self.conn.commit()
        self._embeddings_cache[passage_id] = embedding
        return True

    def embed_all_pending(self) -> int:
        """Embed all approved passages without embeddings. Returns count."""
        rows = self.conn.execute("""
            SELECT id FROM passages
            WHERE status = 'approved' AND embedding IS NULL
        """).fetchall()

        count = 0
        for (passage_id,) in rows:
            if self.embed_passage(passage_id):
                count += 1

        return count

    def search(self, query: str, k: int = 3, jurisdiction: str = "") -> list[Passage]:
        """Search for passages matching the query.

        Args:
            query: Natural language query
            k: Number of results to return
            jurisdiction: Optional filter by jurisdiction (e.g., "TX", "federal")

        Returns:
            List of Passage objects sorted by relevance score
        """
        # Get query embedding
        query_embedding = get_embedding(query)
        if query_embedding is None:
            log.warning("Could not get query embedding, falling back to text search")
            return self._text_search(query, k, jurisdiction)

        self._load_embeddings()

        if not self._embeddings_cache:
            return self._text_search(query, k, jurisdiction)

        # Get all approved passages with embeddings
        jurisdiction_filter = ""
        params = []
        if jurisdiction:
            # Look up sources by jurisdiction
            from mortgage.knowledge.ingest import load_sources
            sources, _ = load_sources()
            source_ids = [s.id for s in sources if s.jurisdiction.lower() == jurisdiction.lower()]
            if source_ids:
                placeholders = ",".join("?" * len(source_ids))
                jurisdiction_filter = f"AND source_id IN ({placeholders})"
                params = source_ids

        query_sql = f"""
            SELECT id, source_id, content, title, section, url
            FROM passages
            WHERE status = 'approved' AND embedding IS NOT NULL
            {jurisdiction_filter}
        """

        rows = self.conn.execute(query_sql, params).fetchall()

        # Score all passages
        scored = []
        for row in rows:
            passage_id = row[0]
            if passage_id in self._embeddings_cache:
                score = cosine_similarity(query_embedding, self._embeddings_cache[passage_id])
                scored.append((score, row))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Return top k
        results = []
        for score, row in scored[:k]:
            results.append(Passage(
                id=row[0],
                source_id=row[1],
                content=row[2],
                title=row[3],
                section=row[4],
                url=row[5],
                score=score
            ))

        return results

    def _text_search(self, query: str, k: int, jurisdiction: str) -> list[Passage]:
        """Fallback text search when embeddings unavailable."""
        words = query.lower().split()
        if not words:
            return []

        # Build LIKE conditions
        conditions = " OR ".join(["content LIKE ?" for _ in words])
        params = [f"%{w}%" for w in words]

        jurisdiction_filter = ""
        if jurisdiction:
            from mortgage.knowledge.ingest import load_sources
            sources, _ = load_sources()
            source_ids = [s.id for s in sources if s.jurisdiction.lower() == jurisdiction.lower()]
            if source_ids:
                placeholders = ",".join("?" * len(source_ids))
                jurisdiction_filter = f"AND source_id IN ({placeholders})"
                params.extend(source_ids)

        query_sql = f"""
            SELECT id, source_id, content, title, section, url
            FROM passages
            WHERE status = 'approved' AND ({conditions})
            {jurisdiction_filter}
            LIMIT ?
        """
        params.append(k)

        rows = self.conn.execute(query_sql, params).fetchall()

        results = []
        for row in rows:
            # Simple scoring based on word matches
            content_lower = row[2].lower()
            score = sum(1 for w in words if w in content_lower) / len(words)
            results.append(Passage(
                id=row[0],
                source_id=row[1],
                content=row[2],
                title=row[3],
                section=row[4],
                url=row[5],
                score=score
            ))

        return sorted(results, key=lambda p: p.score, reverse=True)

    def get_passage_count(self) -> dict:
        """Get count of passages by status."""
        rows = self.conn.execute("""
            SELECT status, COUNT(*) FROM passages GROUP BY status
        """).fetchall()
        return {status: count for status, count in rows}


# Module-level singleton for convenience
_index: Optional[KnowledgeIndex] = None


def get_index() -> KnowledgeIndex:
    """Get or create the knowledge index singleton."""
    global _index
    if _index is None:
        _index = KnowledgeIndex()
    return _index


def search(query: str, k: int = 3, jurisdiction: str = "") -> list[Passage]:
    """Search for regulatory passages matching the query.

    This is the main entry point for the search_regulations tool.

    Args:
        query: Natural language query about regulations
        k: Number of results to return (default 3)
        jurisdiction: Optional filter (e.g., "TX", "federal", "internal")

    Returns:
        List of Passage objects with content and citations
    """
    try:
        return get_index().search(query, k, jurisdiction)
    except FileNotFoundError:
        log.warning("Knowledge database not found. Run ingest first.")
        return []
    except Exception as e:
        log.error("Search failed: %s", e)
        return []


def reset_index() -> None:
    """Reset the singleton index (for testing)."""
    global _index
    _index = None
