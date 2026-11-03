"""
Tool-layer wrapper around rag/retriever.py. Graph nodes import from here,
not from rag/ directly, so a change to the retriever's signature only
touches this file.
"""
from rag.retriever import search_policy


def search_policy_tool(query: str, document_id: str | None = None) -> dict:
    """
    Standard tool contract: {"success": bool, "passages": [...], ...}.
    document_id, when given, scopes retrieval to one policy document
    (e.g. "refund_policy_v1") instead of searching across all five.
    """
    filters = {"document_id": document_id} if document_id else None
    return search_policy(query, top_k=3, filters=filters)