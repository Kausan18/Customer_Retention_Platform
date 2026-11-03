"""
Query-time retrieval: embed a query, compare against stored policy chunk
embeddings, return top-k passages with citations.
"""
import json

from app.db.database import fetch_all
from rag.embedder import embed_chunks


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def search_policy(query: str, top_k: int = 3, filters: dict | None = None) -> dict:
    """
    Returns:
      {"success": True, "passages": [{"content", "document", "section", "similarity_score"}, ...]}
      or
      {"success": False, "error_code": "...", "passages": []}
    """
    if not query or not query.strip():
        return {"success": False, "error_code": "EMPTY_QUERY", "passages": []}

    sql = "SELECT id, document_id, section_id, content, embedding FROM policy_chunks"
    params = []
    if filters and filters.get("document_id"):
        sql += " WHERE document_id = ?"
        params.append(filters["document_id"])

    rows = fetch_all(sql, params)
    if not rows:
        return {"success": False, "error_code": "NO_POLICY_DATA", "passages": []}

    try:
        query_vec = embed_chunks([query])[0]
    except Exception as e:
        return {"success": False, "error_code": "EMBEDDING_FAILED", "passages": [], "message": str(e)}

    scored = []
    for row in rows:
        try:
            chunk_vec = json.loads(row["embedding"])
        except (TypeError, json.JSONDecodeError):
            continue
        score = _dot(query_vec, chunk_vec)
        scored.append((score, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[:top_k]

    passages = [
        {
            "content": row["content"],
            "document": row["document_id"],
            "section": row["section_id"],
            "similarity_score": round(score, 4),
        }
        for score, row in top
    ]

    return {"success": True, "passages": passages}