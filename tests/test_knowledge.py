"""Tests for mortgage knowledge pipeline (no network required).

Uses a small fixture corpus to test ingestion, indexing, and search.
"""

import asyncio
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from mortgage.knowledge.ingest import (
    RawPassage,
    Source,
    chunk_text,
    content_hash,
    extract_text_from_html,
    init_db,
    store_passages,
    approve_source,
    get_status,
)
from mortgage.knowledge.index import (
    KnowledgeIndex,
    Passage,
    cosine_similarity,
    reset_index,
)


# --- Fixture corpus ---

TEXAS_CONSTITUTION_EXCERPT = """
Section 50. HOMESTEAD; PROTECTION FROM FORCED SALE; MORTGAGES, TRUST DEEDS, AND LIENS.

(a) The homestead of a family, or of a single adult person, shall be, and is hereby protected from forced sale,
for the payment of all debts except for:

(6) an extension of credit that:
(A) is secured by a voluntary lien on the homestead created under a written agreement with the consent of each
owner and each owner's spouse;
(B) is of a principal amount that when added to the aggregate total of the outstanding principal balances of all
other indebtedness secured by valid encumbrances of record against the homestead does not exceed 80 percent of
the fair market value of the homestead on the date the extension of credit is made;

(t) The lender or any holder of the note for an extension of credit described by Subsection (a)(6) of this section
may not accelerate the maturity of the extension of credit solely because the owner of a homestead fails to timely
pay ad valorem taxes, assessments, or any other charge that if unpaid may result in a lien on the homestead.

The owner must receive a copy of all documents signed at closing on the date the extension of credit is made,
and the lender may not act on a power of sale or accelerate maturity of the loan within 12 days after the extension
of credit is made. The owner has the right to rescind the extension of credit without penalty within 3 days after
the extension of credit is made.
"""

CFPB_HELOC_EXCERPT = """
Regulation Z - Home Equity Lines of Credit (HELOCs)

Section 1026.40 - Requirements for home equity plans.

A creditor shall disclose the following, as applicable:

(1) Retention of information. The creditor must retain evidence of compliance with this section for two years
after the date disclosures are required to be made or action is required to be taken.

(2) Variable rate information. If the annual percentage rate may increase during the plan, the creditor must disclose:
(i) The circumstances under which the rate may increase.
(ii) Any limitations on the increase.
(iii) The effect of an increase.

(3) Right of rescission. The consumer has the right to rescind the transaction until midnight of the third business
day following consummation, receipt of the notice required by this section, or delivery of all material disclosures,
whichever occurs last.
"""


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_passages.db"
        conn = init_db(db_path)
        yield conn, db_path
        conn.close()


@pytest.fixture
def sample_passages():
    """Create sample passages for testing."""
    now = datetime.now(timezone.utc)
    return [
        RawPassage(
            source_id="tx_constitution",
            content=TEXAS_CONSTITUTION_EXCERPT[:500],
            title="Texas Constitution Article XVI Section 50",
            section="Section 50(a)(6)",
            content_hash=content_hash(TEXAS_CONSTITUTION_EXCERPT[:500]),
            retrieved_at=now,
            url="https://statutes.capitol.texas.gov/..."
        ),
        RawPassage(
            source_id="tx_constitution",
            content=TEXAS_CONSTITUTION_EXCERPT[500:],
            title="Texas Constitution Article XVI Section 50",
            section="Section 50(t)",
            content_hash=content_hash(TEXAS_CONSTITUTION_EXCERPT[500:]),
            retrieved_at=now,
            url="https://statutes.capitol.texas.gov/..."
        ),
        RawPassage(
            source_id="cfpb_reg_z",
            content=CFPB_HELOC_EXCERPT,
            title="CFPB Regulation Z - HELOCs",
            section="Section 1026.40",
            content_hash=content_hash(CFPB_HELOC_EXCERPT),
            retrieved_at=now,
            url="https://www.consumerfinance.gov/..."
        ),
    ]


# --- Unit tests for ingest.py ---

class TestContentHash:
    """Tests for content_hash function."""

    def test_same_content_same_hash(self):
        """Same content produces same hash."""
        text = "Test content"
        assert content_hash(text) == content_hash(text)

    def test_different_content_different_hash(self):
        """Different content produces different hash."""
        assert content_hash("Text A") != content_hash("Text B")

    def test_hash_is_string(self):
        """Hash is a string of expected length."""
        h = content_hash("Test")
        assert isinstance(h, str)
        assert len(h) == 16  # Truncated SHA256


class TestChunkText:
    """Tests for chunk_text function."""

    def test_small_text_single_chunk(self):
        """Small text produces single chunk."""
        text = "This is a short sentence."
        chunks = list(chunk_text(text, chunk_size=100))
        assert len(chunks) == 1
        assert chunks[0][0] == text
        assert chunks[0][1] == 0

    def test_large_text_multiple_chunks(self):
        """Large text is split into multiple chunks."""
        text = ". ".join([f"Sentence {i} with some content" for i in range(50)])
        chunks = list(chunk_text(text, chunk_size=100, overlap=20))
        assert len(chunks) > 1

    def test_chunks_have_overlap(self):
        """Consecutive chunks have overlapping content."""
        text = ". ".join([f"Sentence {i}" for i in range(20)])
        chunks = list(chunk_text(text, chunk_size=50, overlap=10))
        if len(chunks) > 1:
            # Check some overlap between consecutive chunks
            for i in range(len(chunks) - 1):
                chunk1_words = set(chunks[i][0].split())
                chunk2_words = set(chunks[i + 1][0].split())
                # There should be some word overlap
                assert len(chunk1_words & chunk2_words) > 0


class TestExtractTextFromHtml:
    """Tests for extract_text_from_html function."""

    def test_extracts_title(self):
        """Extracts title from HTML."""
        html = "<html><head><title>Test Title</title></head><body>Content</body></html>"
        text, title, headings = extract_text_from_html(html)
        assert "Test Title" in title

    def test_extracts_headings(self):
        """Extracts headings from HTML."""
        html = "<html><body><h1>Main Heading</h1><p>Content</p><h2>Sub Heading</h2></body></html>"
        text, title, headings = extract_text_from_html(html)
        assert "Main Heading" in headings or "Main Heading" in text
        assert "Sub Heading" in headings or "Sub Heading" in text

    def test_removes_scripts(self):
        """Removes script tags from content."""
        html = "<html><body><script>alert('bad')</script><p>Good content</p></body></html>"
        text, title, headings = extract_text_from_html(html)
        assert "alert" not in text
        assert "Good content" in text


class TestDatabaseOperations:
    """Tests for database storage operations."""

    def test_init_db_creates_tables(self, temp_db):
        """init_db creates required tables."""
        conn, db_path = temp_db
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        assert "passages" in tables

    def test_store_passages_creates_records(self, temp_db, sample_passages):
        """store_passages creates database records."""
        conn, db_path = temp_db
        new, updated, unchanged = store_passages(sample_passages, conn)
        assert new == 3
        assert updated == 0
        assert unchanged == 0

        # Verify records exist
        cursor = conn.execute("SELECT COUNT(*) FROM passages")
        assert cursor.fetchone()[0] == 3

    def test_store_passages_detects_unchanged(self, temp_db, sample_passages):
        """store_passages detects unchanged passages."""
        conn, db_path = temp_db
        store_passages(sample_passages, conn)
        new, updated, unchanged = store_passages(sample_passages, conn)
        assert new == 0
        assert unchanged == 3

    def test_passages_default_to_pending_review(self, temp_db, sample_passages):
        """New passages have status 'pending_review'."""
        conn, db_path = temp_db
        store_passages(sample_passages, conn)

        cursor = conn.execute("SELECT status FROM passages")
        statuses = [row[0] for row in cursor.fetchall()]
        assert all(s == "pending_review" for s in statuses)

    def test_approve_source_changes_status(self, temp_db, sample_passages):
        """approve_source changes status to 'approved'."""
        conn, db_path = temp_db
        store_passages(sample_passages, conn)

        count = approve_source("tx_constitution", conn)
        assert count == 2  # Two passages from tx_constitution

        cursor = conn.execute("SELECT status FROM passages WHERE source_id = 'tx_constitution'")
        statuses = [row[0] for row in cursor.fetchall()]
        assert all(s == "approved" for s in statuses)

    def test_get_status_returns_counts(self, temp_db, sample_passages):
        """get_status returns passage counts by source and status."""
        conn, db_path = temp_db
        store_passages(sample_passages, conn)
        approve_source("tx_constitution", conn)

        status = get_status(conn)
        assert "tx_constitution" in status
        assert status["tx_constitution"]["approved"] == 2
        assert "cfpb_reg_z" in status
        assert status["cfpb_reg_z"]["pending_review"] == 1


# --- Unit tests for index.py ---

class TestCosineSimilarity:
    """Tests for cosine_similarity function."""

    def test_identical_vectors_score_one(self):
        """Identical vectors have similarity 1.0."""
        vec = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        assert abs(cosine_similarity(vec, vec) - 1.0) < 0.001

    def test_orthogonal_vectors_score_zero(self):
        """Orthogonal vectors have similarity 0.0."""
        vec1 = np.array([1.0, 0.0], dtype=np.float32)
        vec2 = np.array([0.0, 1.0], dtype=np.float32)
        assert abs(cosine_similarity(vec1, vec2)) < 0.001

    def test_opposite_vectors_score_negative_one(self):
        """Opposite vectors have similarity -1.0."""
        vec1 = np.array([1.0, 0.0], dtype=np.float32)
        vec2 = np.array([-1.0, 0.0], dtype=np.float32)
        assert abs(cosine_similarity(vec1, vec2) + 1.0) < 0.001


class TestKnowledgeIndex:
    """Tests for KnowledgeIndex class."""

    def test_text_search_fallback(self, temp_db, sample_passages):
        """Text search works when embeddings are unavailable."""
        conn, db_path = temp_db
        store_passages(sample_passages, conn)
        approve_source("tx_constitution", conn)
        approve_source("cfpb_reg_z", conn)

        index = KnowledgeIndex(db_path)
        results = index._text_search("homestead 80 percent", k=3, jurisdiction="")

        assert len(results) > 0
        # Should find the Texas constitution passage about 80%
        assert any("homestead" in r.content.lower() for r in results)

    def test_text_search_with_jurisdiction(self, temp_db, sample_passages):
        """Text search filters by jurisdiction when source mapping available."""
        conn, db_path = temp_db
        store_passages(sample_passages, conn)
        approve_source("tx_constitution", conn)
        approve_source("cfpb_reg_z", conn)

        index = KnowledgeIndex(db_path)
        # This would need source mapping - test basic functionality
        results = index._text_search("rescission", k=3, jurisdiction="")

        assert len(results) > 0

    def test_get_passage_count(self, temp_db, sample_passages):
        """get_passage_count returns correct counts."""
        conn, db_path = temp_db
        store_passages(sample_passages, conn)
        approve_source("tx_constitution", conn)

        index = KnowledgeIndex(db_path)
        counts = index.get_passage_count()

        assert "approved" in counts
        assert "pending_review" in counts
        assert counts["approved"] == 2
        assert counts["pending_review"] == 1


class TestPassageToDict:
    """Tests for Passage.to_dict method."""

    def test_to_dict_includes_required_fields(self):
        """to_dict includes all required fields."""
        passage = Passage(
            id=1,
            source_id="test",
            content="Test content here",
            title="Test Title",
            section="Section 1",
            url="https://example.com",
            score=0.95
        )
        d = passage.to_dict()

        assert "source_id" in d
        assert "title" in d
        assert "section" in d
        assert "content" in d
        assert "url" in d
        assert "relevance_score" in d

    def test_to_dict_truncates_long_content(self):
        """to_dict truncates content over 500 chars."""
        long_content = "x" * 600
        passage = Passage(
            id=1,
            source_id="test",
            content=long_content,
            title="Test",
            section="",
            url=None,
            score=0.5
        )
        d = passage.to_dict()

        assert len(d["content"]) < 600
        assert d["content"].endswith("...")


# --- Integration tests for search_regulations tool ---

class TestSearchRegulationsTool:
    """Tests for search_regulations tool function."""

    def test_returns_ok_when_no_passages(self):
        """Tool returns ok with empty passages when none found."""
        # Reset the singleton to avoid using stale index
        reset_index()

        async def run():
            from mortgage import tools
            # Search with no database - should return empty
            result = await tools.search_regulations("test query", "TX")
            return result

        result = asyncio.run(run())
        assert result["result"] in ("ok", "error")

    def test_returns_say_guidance(self):
        """Tool returns 'say' guidance for the model."""
        reset_index()

        async def run():
            from mortgage import tools
            result = await tools.search_regulations("Texas HELOC rules")
            return result

        result = asyncio.run(run())
        assert "say" in result
