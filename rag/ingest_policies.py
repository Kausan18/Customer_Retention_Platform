"""
Ingests all policy .md files in policies/ into policy_documents and
policy_chunks. Idempotent — re-running clears old chunks for a document
and reinserts, so editing a policy file and re-running picks up changes
with no manual cleanup.

Run with: python rag/ingest_policies.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import get_connection
from rag.embedder import chunk_document, embed_chunks

POLICIES_DIR = PROJECT_ROOT / "policies"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS policy_documents (
  id              TEXT PRIMARY KEY,
  version         TEXT,
  effective_from  TEXT,
  source_path     TEXT,
  ingested_at     TEXT
);

CREATE TABLE IF NOT EXISTS policy_chunks (
  id            TEXT PRIMARY KEY,
  document_id   TEXT NOT NULL REFERENCES policy_documents(id),
  section_id    TEXT NOT NULL,
  content       TEXT NOT NULL,
  embedding     BLOB NOT NULL,
  char_count    INTEGER
);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA_SQL)


def ingest_file(conn, filepath: Path) -> tuple[str, int]:
    chunks, meta = chunk_document(str(filepath))
    embeddings = embed_chunks([c["content"] for c in chunks])

    document_id = meta["document_id"]
    now = datetime.now(timezone.utc).isoformat()

    conn.execute("DELETE FROM policy_chunks WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM policy_documents WHERE id = ?", (document_id,))

    conn.execute(
        "INSERT INTO policy_documents (id, version, effective_from, source_path, ingested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (document_id, meta["version"], meta["effective_from"], str(filepath), now),
    )

    for chunk, embedding in zip(chunks, embeddings):
        chunk_id = f"{chunk['document_id']}__{chunk['section_id']}"
        conn.execute(
            "INSERT INTO policy_chunks (id, document_id, section_id, content, embedding, char_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                chunk["document_id"],
                chunk["section_id"],
                chunk["content"],
                json.dumps(embedding),
                chunk["char_count"],
            ),
        )

    return document_id, len(chunks)


def main():
    if not POLICIES_DIR.exists():
        print(f"ERROR: {POLICIES_DIR} does not exist.")
        sys.exit(1)

    md_files = sorted(POLICIES_DIR.glob("*.md"))
    if not md_files:
        print(f"ERROR: no .md files found in {POLICIES_DIR}.")
        sys.exit(1)

    total_chunks = 0
    results = []

    with get_connection() as conn:
        ensure_schema(conn)
        for filepath in md_files:
            try:
                document_id, chunk_count = ingest_file(conn, filepath)
                results.append((filepath.name, document_id, chunk_count, None))
                total_chunks += chunk_count
            except ValueError as e:
                results.append((filepath.name, None, 0, str(e)))

    print("=== Policy Ingestion Results ===")
    for filename, document_id, chunk_count, error in results:
        if error:
            print(f"  FAIL | {filename}: {error}")
        else:
            print(f"  OK   | {document_id}: {chunk_count} chunks ingested  ({filename})")

    print(f"\nTotal chunks across all documents: {total_chunks}")

    if any(error for _, _, _, error in results):
        sys.exit(1)


if __name__ == "__main__":
    main()