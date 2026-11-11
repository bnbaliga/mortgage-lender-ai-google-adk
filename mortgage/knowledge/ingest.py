"""Document ingestion pipeline for regulatory sources.

Fetches documents from approved sources, extracts text, splits into passages,
and stores with metadata for embedding and search.

Usage:
    uv run python -m mortgage.knowledge.ingest          # Ingest all sources
    uv run python -m mortgage.knowledge.ingest --source tx_constitution_art16_sec50
    uv run python -m mortgage.knowledge.ingest approve <source_id>
    uv run python -m mortgage.knowledge.ingest status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import yaml

log = logging.getLogger("mortgage.knowledge.ingest")

# Paths
KNOWLEDGE_DIR = Path(__file__).parent
SOURCES_PATH = KNOWLEDGE_DIR / "sources.yaml"
DB_PATH = KNOWLEDGE_DIR / "passages.db"
PROJECT_ROOT = KNOWLEDGE_DIR.parents[1]


@dataclass
class Source:
    """An approved regulatory source."""
    id: str
    name: str
    jurisdiction: str
    topics: list[str]
    doc_type: str
    url: Optional[str] = None
    path: Optional[str] = None
    notes: str = ""


@dataclass
class RawPassage:
    """A text passage extracted from a source."""
    source_id: str
    content: str
    title: str
    section: str
    content_hash: str
    retrieved_at: datetime
    url: Optional[str] = None
    page_num: Optional[int] = None


def load_sources() -> tuple[list[Source], dict]:
    """Load sources and settings from sources.yaml."""
    with open(SOURCES_PATH, "r") as f:
        data = yaml.safe_load(f)

    sources = []
    for s in data.get("sources", []):
        sources.append(Source(
            id=s["id"],
            name=s["name"],
            jurisdiction=s["jurisdiction"],
            topics=s.get("topics", []),
            doc_type=s.get("doc_type", "unknown"),
            url=s.get("url"),
            path=s.get("path"),
            notes=s.get("notes", "")
        ))

    settings = data.get("settings", {})
    return sources, settings


def content_hash(text: str) -> str:
    """Generate a content hash for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (4 chars per token)."""
    return len(text) // 4


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> Iterator[tuple[str, int]]:
    """Split text into overlapping chunks of approximately chunk_size tokens.

    Yields (chunk_text, chunk_index) tuples.
    """
    # Split into sentences (rough)
    sentences = re.split(r'(?<=[.!?])\s+', text)

    current_chunk = []
    current_tokens = 0
    chunk_idx = 0

    for sentence in sentences:
        sent_tokens = _estimate_tokens(sentence)

        if current_tokens + sent_tokens > chunk_size and current_chunk:
            # Yield current chunk
            yield " ".join(current_chunk), chunk_idx
            chunk_idx += 1

            # Keep overlap
            overlap_tokens = 0
            overlap_start = len(current_chunk)
            for i in range(len(current_chunk) - 1, -1, -1):
                overlap_tokens += _estimate_tokens(current_chunk[i])
                if overlap_tokens >= overlap:
                    overlap_start = i
                    break

            current_chunk = current_chunk[overlap_start:]
            current_tokens = sum(_estimate_tokens(s) for s in current_chunk)

        current_chunk.append(sentence)
        current_tokens += sent_tokens

    # Yield remaining
    if current_chunk:
        yield " ".join(current_chunk), chunk_idx


class RobotsChecker:
    """Check robots.txt compliance."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._parsers: dict[str, RobotFileParser] = {}

    def can_fetch(self, url: str) -> bool:
        """Check if we can fetch this URL according to robots.txt."""
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        if base not in self._parsers:
            rp = RobotFileParser()
            robots_url = f"{base}/robots.txt"
            try:
                rp.set_url(robots_url)
                rp.read()
            except Exception:
                # If we can't read robots.txt, assume allowed
                pass
            self._parsers[base] = rp

        return self._parsers[base].can_fetch(self.user_agent, url)


def extract_text_from_html(html: str) -> tuple[str, str, list[str]]:
    """Extract text from HTML, return (text, title, section_headings)."""
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, include_tables=True)
        if text:
            # Try to get title
            title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else ""

            # Extract headings for sections
            headings = re.findall(r'<h[1-6][^>]*>([^<]+)</h[1-6]>', html, re.IGNORECASE)

            return text, title, headings
    except ImportError:
        pass

    # Fallback to BeautifulSoup
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # Remove script and style
        for tag in soup(["script", "style"]):
            tag.decompose()

        title = soup.title.string if soup.title else ""
        headings = [h.get_text(strip=True) for h in soup.find_all(re.compile(r'^h[1-6]$'))]
        text = soup.get_text(separator=" ", strip=True)

        return text, title, headings
    except ImportError:
        # Last resort: regex stripping
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text, "", []


def extract_text_from_pdf(pdf_path: Path) -> tuple[str, str, list[str]]:
    """Extract text from PDF file."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(pdf_path))
        text_parts = []
        for page in reader.pages:
            text_parts.append(page.extract_text() or "")

        text = "\n\n".join(text_parts)
        title = pdf_path.stem.replace("_", " ").replace("-", " ").title()
        return text, title, []
    except ImportError:
        log.warning("pypdf not installed, cannot process PDF: %s", pdf_path)
        return "", "", []
    except Exception as e:
        log.error("Error reading PDF %s: %s", pdf_path, e)
        return "", "", []


def fetch_url(url: str, settings: dict, robots: RobotsChecker) -> Optional[str]:
    """Fetch URL content with rate limiting and robots.txt compliance."""
    if settings.get("respect_robots_txt", True) and not robots.can_fetch(url):
        log.warning("Blocked by robots.txt: %s", url)
        return None

    headers = {"User-Agent": settings.get("user_agent", "KnowledgeBot/1.0")}
    rate_limit = settings.get("rate_limit_seconds", 2.0)

    try:
        time.sleep(rate_limit)  # Rate limiting
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        log.error("Error fetching %s: %s", url, e)
        return None


def ingest_source(source: Source, settings: dict, robots: RobotsChecker) -> list[RawPassage]:
    """Ingest a single source, returning extracted passages."""
    passages = []
    now = datetime.now(timezone.utc)
    chunk_size = settings.get("chunk_size_tokens", 800)
    chunk_overlap = settings.get("chunk_overlap_tokens", 100)

    if source.url:
        log.info("Fetching URL: %s", source.url)
        html = fetch_url(source.url, settings, robots)
        if html:
            text, title, headings = extract_text_from_html(html)
            title = title or source.name

            for chunk, idx in chunk_text(text, chunk_size, chunk_overlap):
                if len(chunk.strip()) < 50:
                    continue

                section = headings[min(idx, len(headings) - 1)] if headings else ""
                passages.append(RawPassage(
                    source_id=source.id,
                    content=chunk.strip(),
                    title=title,
                    section=section,
                    content_hash=content_hash(chunk),
                    retrieved_at=now,
                    url=source.url
                ))

    elif source.path:
        # Local folder
        folder = PROJECT_ROOT / source.path
        if folder.exists():
            for pdf_file in folder.glob("**/*.pdf"):
                log.info("Processing PDF: %s", pdf_file)
                text, title, _ = extract_text_from_pdf(pdf_file)
                title = title or pdf_file.stem

                for chunk, idx in chunk_text(text, chunk_size, chunk_overlap):
                    if len(chunk.strip()) < 50:
                        continue

                    passages.append(RawPassage(
                        source_id=source.id,
                        content=chunk.strip(),
                        title=title,
                        section=f"Page {idx + 1}",
                        content_hash=content_hash(chunk),
                        retrieved_at=now,
                        page_num=idx
                    ))

    log.info("Extracted %d passages from %s", len(passages), source.id)
    return passages


# ---------- Database storage ----------

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Initialize SQLite database for passages."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS passages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            content TEXT NOT NULL,
            title TEXT,
            section TEXT,
            content_hash TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            url TEXT,
            page_num INTEGER,
            status TEXT DEFAULT 'pending_review',
            embedding BLOB,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, content_hash)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_source_id ON passages(source_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_status ON passages(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_content_hash ON passages(content_hash)
    """)
    conn.commit()
    return conn


def store_passages(passages: list[RawPassage], conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Store passages, returning (new_count, updated_count, unchanged_count)."""
    new_count = 0
    updated_count = 0
    unchanged_count = 0

    for p in passages:
        # Check if exists
        existing = conn.execute(
            "SELECT id, content_hash, status FROM passages WHERE source_id = ? AND content_hash = ?",
            (p.source_id, p.content_hash)
        ).fetchone()

        if existing:
            unchanged_count += 1
        else:
            # Check if same source has old version
            old = conn.execute(
                "SELECT id FROM passages WHERE source_id = ? AND content = ?",
                (p.source_id, p.content)
            ).fetchone()

            if old:
                # Content hash changed but content same - update hash
                conn.execute(
                    "UPDATE passages SET content_hash = ?, retrieved_at = ? WHERE id = ?",
                    (p.content_hash, p.retrieved_at.isoformat(), old[0])
                )
                updated_count += 1
            else:
                # New passage - mark as pending_review
                conn.execute("""
                    INSERT INTO passages (source_id, content, title, section, content_hash,
                                         retrieved_at, url, page_num, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_review')
                """, (p.source_id, p.content, p.title, p.section, p.content_hash,
                      p.retrieved_at.isoformat(), p.url, p.page_num))
                new_count += 1

    conn.commit()
    return new_count, updated_count, unchanged_count


def approve_source(source_id: str, conn: sqlite3.Connection) -> int:
    """Approve all passages from a source. Returns count approved."""
    cursor = conn.execute(
        "UPDATE passages SET status = 'approved' WHERE source_id = ? AND status = 'pending_review'",
        (source_id,)
    )
    conn.commit()
    return cursor.rowcount


def get_status(conn: sqlite3.Connection) -> dict:
    """Get status of all passages."""
    rows = conn.execute("""
        SELECT source_id, status, COUNT(*) as count
        FROM passages
        GROUP BY source_id, status
        ORDER BY source_id, status
    """).fetchall()

    result = {}
    for source_id, status, count in rows:
        if source_id not in result:
            result[source_id] = {}
        result[source_id][status] = count

    return result


# ---------- CLI ----------

def main():
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Ingest regulatory documents")
    parser.add_argument("command", nargs="?", default="ingest",
                        choices=["ingest", "approve", "status"],
                        help="Command to run")
    parser.add_argument("source_id", nargs="?", help="Source ID for approve command")
    parser.add_argument("--source", "-s", help="Specific source ID to ingest")
    args = parser.parse_args()

    conn = init_db()
    sources, settings = load_sources()

    if args.command == "status":
        status = get_status(conn)
        if not status:
            print("No passages in database.")
        else:
            print("\nPassage Status:")
            print("-" * 50)
            for source_id, statuses in status.items():
                print(f"\n{source_id}:")
                for status_name, count in statuses.items():
                    print(f"  {status_name}: {count}")
        return

    if args.command == "approve":
        if not args.source_id:
            print("Error: source_id required for approve command")
            print("Usage: uv run python -m mortgage.knowledge.ingest approve <source_id>")
            return

        count = approve_source(args.source_id, conn)
        print(f"Approved {count} passages from {args.source_id}")
        return

    # Ingest command
    user_agent = settings.get("user_agent", "KnowledgeBot/1.0")
    robots = RobotsChecker(user_agent)

    if args.source:
        sources = [s for s in sources if s.id == args.source]
        if not sources:
            print(f"Error: Source '{args.source}' not found")
            return

    total_new = 0
    total_updated = 0
    total_unchanged = 0

    for source in sources:
        try:
            passages = ingest_source(source, settings, robots)
            new, updated, unchanged = store_passages(passages, conn)
            total_new += new
            total_updated += updated
            total_unchanged += unchanged
            print(f"{source.id}: {new} new, {updated} updated, {unchanged} unchanged")
        except Exception as e:
            log.error("Error processing %s: %s", source.id, e)

    print(f"\nTotal: {total_new} new, {total_updated} updated, {total_unchanged} unchanged")
    if total_new > 0:
        print("Run 'approve <source_id>' to make new passages searchable.")


if __name__ == "__main__":
    main()
